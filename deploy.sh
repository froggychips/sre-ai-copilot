#!/usr/bin/env bash
# Деплой sre-ai-copilot в текущий kubectl-контекст.
#
# Использование:
#   IMAGE_TAG=1.0.0-rc.11 ./deploy.sh [namespace]                  # WO-кластер (Nexus)
#   IMAGE_REPO=ghcr.io/froggychips/sre-ai-copilot \
#     IMAGE_TAG=<git-sha> ./deploy.sh [namespace]                  # образ из CI
#
# Про registry. Раньше скрипт хардкодил ghcr и git-sha, потому что туда
# публикует CI. В WO-кластере это не работало ВООБЩЕ: поды тянут из Nexus
# (docker.lastoasisgame.com), а imagePullSecrets для ghcr там нет — запуск
# упирался в ImagePullBackOff на Job-е миграций. Реальные выкаты шли мимо
# скрипта, руками через `kubectl set image`, и репозиторий полтора месяца
# описывал процесс, которого не существует. Теперь registry — параметр.
set -euo pipefail

NAMESPACE="${1:-sre-ai}"
IMAGE_REPO="${IMAGE_REPO:-docker.lastoasisgame.com/wo/sre-ai-copilot}"
IMAGE_TAG="${IMAGE_TAG:-}"
# Порог простоя транзакции, при котором миграции опасно накатывать «на живую».
LONG_TX_SECONDS="${LONG_TX_SECONDS:-60}"

if [[ -z "${IMAGE_TAG}" ]]; then
    echo "ERROR: задай IMAGE_TAG (тега :latest не существует ни в одном registry)" >&2
    echo "  WO-кластер:  IMAGE_TAG=1.0.0-rc.N ./deploy.sh" >&2
    echo "  образ из CI: IMAGE_REPO=ghcr.io/froggychips/sre-ai-copilot IMAGE_TAG=<git-sha> ./deploy.sh" >&2
    exit 1
fi

IMAGE="${IMAGE_REPO}:${IMAGE_TAG}"
echo "Образ: ${IMAGE}"
echo "Namespace: ${NAMESPACE}"

apply_with_image() {
    local manifest="$1"
    sed "s|IMAGE_PLACEHOLDER|${IMAGE}|g" "${manifest}" \
        | kubectl -n "${NAMESPACE}" apply -f -
}

# ── 1. Секреты ────────────────────────────────────────────────────────────
# НИКОГДА не применяем k8s/base/secrets.yaml: это шаблон с REPLACE_ME.
# Раньше скрипт делал `kubectl apply -f k8s/base/secrets.yaml` и ЗАТИРАЛ
# боевой Secret плейсхолдерами — мина, взрывающаяся при следующем рестарте
# pod-ов. Теперь только проверяем, что Secret уже существует
# (SealedSecrets / ExternalSecrets / secrets.local.yaml — см. k8s/base/).
if ! kubectl -n "${NAMESPACE}" get secret sre-ai-secrets >/dev/null 2>&1; then
    echo "ERROR: Secret sre-ai-secrets не найден в ns ${NAMESPACE}." >&2
    echo "Создай его до деплоя (см. k8s/base/secrets.yaml — только как шаблон:" >&2
    echo "  cp k8s/base/secrets.yaml k8s/base/secrets.local.yaml   # заполнить и применить" >&2
    echo "  либо SealedSecrets/ExternalSecrets — k8s/base/secrets.*.yaml.example)." >&2
    exit 1
fi

# ── 2. RBAC ───────────────────────────────────────────────────────────────
# ServiceAccount sre-ai нужен до старта Job/Deployment.
kubectl -n "${NAMESPACE}" apply -f k8s/base/rbac.yaml

# Права проверяем ФАКТИЧЕСКИ, а не по манифесту: 08.08.2026 выяснилось, что
# ClusterRole из репозитория в WO-кластере не применялась вовсе (там свои
# роли), и синк топологии падал на `statefulsets is forbidden` — молча теряя
# все *-db узлы.
missing_rbac=0
for res in deployments statefulsets daemonsets services ingresses; do
    group="apps"
    case "${res}" in
        services) group="" ;;
        ingresses) group="networking.k8s.io" ;;
    esac
    target="${res}${group:+.${group}}"
    if ! kubectl auth can-i list "${target}" \
            --as="system:serviceaccount:${NAMESPACE}:sre-ai" \
            --all-namespaces >/dev/null 2>&1; then
        echo "WARNING: SA sre-ai не может list ${target} — синк топологии потеряет эти объекты" >&2
        missing_rbac=1
    fi
done
if [[ "${missing_rbac}" == "1" ]]; then
    echo "WARNING: RBAC неполный. Деплой продолжается, но граф будет неполным." >&2
fi

# ── 3. Миграции: отдельный Job ДО выката приложения ──────────────────────
# Job immutable по spec → пересоздаём.
#
# Перед накатом смотрим на долгие транзакции. DDL требует ACCESS EXCLUSIVE, и
# если лок занят, он встаёт в очередь и БЛОКИРУЕТ ВСЕХ, кто пришёл после него:
# 08.08.2026 так зависли семь читателей kg_services, приложение стояло 6 минут,
# а осиротевшее соединение пришлось снимать вручную. lock_timeout в самом Job-е
# (PGOPTIONS ниже) ограничивает ущерб, но лучше не начинать вовсе.
long_tx="$(kubectl -n "${NAMESPACE}" exec postgres-0 -- sh -c \
    'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "
     SELECT coalesce(max(extract(epoch from now()-xact_start))::int, 0)
     FROM pg_stat_activity
     WHERE datname = current_database() AND state <> '"'"'idle'"'"'"' 2>/dev/null | tr -d ' \r' || echo 0)"
if [[ "${long_tx:-0}" -gt "${LONG_TX_SECONDS}" ]]; then
    echo "WARNING: в БД есть транзакция возрастом ${long_tx}s (порог ${LONG_TX_SECONDS}s)." >&2
    echo "  Миграция может не взять лок. Если Job упадёт по lock_timeout — заглуши писателей:" >&2
    echo "    kubectl -n ${NAMESPACE} scale deploy copilot-worker copilot-beat --replicas=0" >&2
fi

kubectl -n "${NAMESPACE}" delete job sre-ai-migrate --ignore-not-found
apply_with_image k8s/migrate-job.yaml
echo "Жду завершения миграций (job/sre-ai-migrate)..."
if ! kubectl -n "${NAMESPACE}" wait --for=condition=complete --timeout=300s job/sre-ai-migrate; then
    echo "ERROR: миграции не завершились. Логи:" >&2
    kubectl -n "${NAMESPACE}" logs "job/sre-ai-migrate" --tail=100 >&2 || true
    echo "" >&2
    echo "Если в логах lock_timeout — лок держит долгая транзакция. Заглуши писателей:" >&2
    echo "  kubectl -n ${NAMESPACE} scale deploy copilot-worker copilot-beat --replicas=0" >&2
    echo "  ./deploy.sh ${NAMESPACE}   # повторить" >&2
    echo "  kubectl -n ${NAMESPACE} scale deploy copilot-worker --replicas=2" >&2
    echo "  kubectl -n ${NAMESPACE} scale deploy copilot-beat --replicas=1" >&2
    exit 1
fi

# ── 4. Приложение ─────────────────────────────────────────────────────────
apply_with_image k8s/base/deployment.yaml
apply_with_image k8s/worker.yaml
kubectl -n "${NAMESPACE}" apply -f k8s/networkpolicy.yaml

for d in sre-ai-api copilot-worker copilot-beat; do
    kubectl -n "${NAMESPACE}" rollout status "deploy/${d}" --timeout=300s || true
done

echo "Deploy OK: ${IMAGE} → ns ${NAMESPACE}"
echo "(Опционально: k8s/prometheus-rules.yaml, k8s/vmalertmanagerconfig.yaml,"
echo " k8s/postgres-backup.yaml, k8s/squad-dashboard.yaml — применяются отдельно.)"
