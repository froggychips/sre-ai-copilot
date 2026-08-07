#!/usr/bin/env bash
# Деплой sre-ai-copilot в текущий kubectl-контекст.
#
# Использование:
#   IMAGE_TAG=<git-sha> ./deploy.sh [namespace]
#
# CI публикует ТОЛЬКО ghcr.io/froggychips/sre-ai-copilot:<git-sha>
# (см. .github/workflows/ci.yml) — тега :latest не существует, поэтому
# IMAGE_TAG обязателен, а в манифестах стоит IMAGE_TAG_PLACEHOLDER,
# который подставляется здесь.
set -euo pipefail

NAMESPACE="${1:-sre-ai}"
IMAGE_TAG="${IMAGE_TAG:-}"

if [[ -z "${IMAGE_TAG}" ]]; then
    echo "ERROR: задай IMAGE_TAG=<git-sha> (CI пушит только :<git-sha>, тега :latest нет)" >&2
    exit 1
fi

# kubectl apply манифеста с подстановкой тега образа.
apply_with_tag() {
    local manifest="$1"
    sed "s|IMAGE_TAG_PLACEHOLDER|${IMAGE_TAG}|g" "${manifest}" \
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

# ── 2. RBAC (нужен ServiceAccount sre-ai до старта Job/Deployment) ───────
kubectl -n "${NAMESPACE}" apply -f k8s/base/rbac.yaml

# ── 3. Миграции: отдельный Job ДО выката приложения ──────────────────────
# Раньше был `kubectl exec` в pod, которого при первом деплое ещё нет
# (Deployment применялся шагом позже). Job-манифест существовал, но не
# использовался. Job immutable по spec → пересоздаём.
kubectl -n "${NAMESPACE}" delete job sre-ai-migrate --ignore-not-found
apply_with_tag k8s/migrate-job.yaml
echo "Жду завершения миграций (job/sre-ai-migrate)..."
if ! kubectl -n "${NAMESPACE}" wait --for=condition=complete --timeout=300s job/sre-ai-migrate; then
    echo "ERROR: миграции не завершились. Логи:" >&2
    kubectl -n "${NAMESPACE}" logs "job/sre-ai-migrate" --tail=100 >&2 || true
    exit 1
fi

# ── 4. Приложение ─────────────────────────────────────────────────────────
apply_with_tag k8s/base/deployment.yaml
apply_with_tag k8s/worker.yaml
kubectl -n "${NAMESPACE}" apply -f k8s/networkpolicy.yaml

echo "Deploy OK: ghcr.io/froggychips/sre-ai-copilot:${IMAGE_TAG} → ns ${NAMESPACE}"
echo "(Опционально: k8s/prometheus-rules.yaml, k8s/vmalertmanagerconfig.yaml,"
echo " k8s/postgres-backup.yaml, k8s/squad-dashboard.yaml — применяются отдельно.)"
