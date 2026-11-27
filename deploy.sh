#!/usr/bin/env bash
# Деплой sre-ai-copilot в текущий kubectl-контекст.
#
# Использование:
#   IMAGE_TAG=1.0.0-rc.11 ./deploy.sh [namespace]                  # WO-кластер (Nexus)
#   IMAGE_REPO=ghcr.io/froggychips/sre-ai-copilot \
#     IMAGE_TAG=<git-sha> ./deploy.sh [namespace]                  # образ из CI
#   IMAGE_TAG=X.Y.Z ./deploy.sh --render [namespace]               # только отрендерить
#
# `--render` печатает в stdout ровно те манифесты, которые применил бы деплой,
# с подставленными образом, версией и значениями площадки, и ничего не меняет:
#   IMAGE_TAG=X.Y.Z ./deploy.sh --render | kubectl -n sre-ai diff -f -
# Так сверяется «git против кластера» (scripts/manifest_drift.py --diff делает
# то же самое), и так же CI проверяет, что в рендере не осталось плейсхолдеров.
#
# Значения площадки (не хранятся в публичном git):
#   STATICS_DB_CIDR — CIDR внешнего Statics-Postgres для NetworkPolicy; если не
#     задан, берётся из ConfigMap sre-ai-copilot-site (k8s/site.yaml.example);
#     `none` — правило не создаётся.
#
# Про registry. Раньше скрипт хардкодил ghcr и git-sha, потому что туда
# публикует CI. В WO-кластере это не работало ВООБЩЕ: поды тянут из Nexus
# (docker.lastoasisgame.com), а imagePullSecrets для ghcr там нет — запуск
# упирался в ImagePullBackOff на Job-е миграций. Реальные выкаты шли мимо
# скрипта, руками через `kubectl set image`, и репозиторий полтора месяца
# описывал процесс, которого не существует. Теперь registry — параметр.
set -euo pipefail

RENDER_ONLY=0
if [[ "${1:-}" == "--render" ]]; then
    RENDER_ONLY=1
    shift
fi
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
# В режиме --render stdout — это манифесты, поэтому сообщения идут в stderr.
log() { echo "$@" >&2; }
log "Образ: ${IMAGE}"
log "Namespace: ${NAMESPACE}"

SETTINGS_MANIFEST="k8s/base/settings.yaml"
# Secret-ы, на которые ссылаются Deployment-ы через envFrom
# (k8s/base/secrets.*.yaml.example). Сами Secret-ы скрипт не создаёт и не правит.
ROLE_SECRETS="sre-ai-secrets sre-ai-executor-secrets sre-ai-beat-secrets"

# Хэш ConfigMap с настройками → аннотация checksum/settings в шаблонах подов.
# Под читает envFrom только на старте: без аннотации правка ConfigMap не давала
# бы rollout, и «применено» расходилось бы с «действует».
if command -v sha256sum >/dev/null 2>&1; then
    SETTINGS_CHECKSUM="$(sha256sum "${SETTINGS_MANIFEST}" | cut -c1-16)"
else
    SETTINGS_CHECKSUM="$(shasum -a 256 "${SETTINGS_MANIFEST}" | cut -c1-16)"
fi

# CIDR внешнего Statics-Postgres. Определяется ДО любых изменений в кластере:
# отсутствие значения — отказ, а не молча потерянное egress-правило.
resolve_statics_cidr() {
    if [[ -n "${STATICS_DB_CIDR:-}" ]]; then
        return 0
    fi
    STATICS_DB_CIDR="$(kubectl -n "${NAMESPACE}" get configmap sre-ai-copilot-site \
        -o jsonpath='{.data.STATICS_DB_CIDR}' 2>/dev/null || true)"
    if [[ -z "${STATICS_DB_CIDR}" ]]; then
        log "ERROR: не задан STATICS_DB_CIDR (CIDR внешнего Statics-Postgres для NetworkPolicy)."
        log "  Разово:    STATICS_DB_CIDR=<адрес>/32 IMAGE_TAG=${IMAGE_TAG} ./deploy.sh ${NAMESPACE}"
        log "  Постоянно: kubectl -n ${NAMESPACE} create configmap sre-ai-copilot-site \\"
        log "               --from-literal=STATICS_DB_CIDR=<адрес>/32   (k8s/site.yaml.example)"
        log "  Внешней базы нет: STATICS_DB_CIDR=none — правило не создаётся."
        log "  Действующее значение: kubectl -n ${NAMESPACE} get networkpolicy copilot-network-policy -o yaml | grep -B1 -A4 ipBlock"
        exit 1
    fi
}

# Рендер манифеста в stdout: образ, версия, хэш настроек, значения площадки.
render_manifest() {
    local manifest="$1"
    # BUILD_VERSION вместе с образом: это тег РЕАЛЬНО задеплоенного образа,
    # он уходит в Discord-сообщения. Раньше переменную ставили руками, и в
    # кластере она застыла на 1.0.0-rc.29 — то есть сообщения ссылались на
    # версию, которой в поде уже не было. Подставляем из того же IMAGE_TAG,
    # чтобы разъехаться стало нечем.
    #
    # Первое выражение — CronJob-ы: у них в манифесте `image: ""` с маркером
    # (сырой apply отклоняется API-сервером, см. k8s/ci-deadman.yaml).
    # Последние два — блок NetworkPolicy между маркерами site:STATICS_DB_CIDR:
    # `none` вырезает его целиком.
    local cidr_expr=(-e "s|STATICS_DB_CIDR_PLACEHOLDER|${STATICS_DB_CIDR:-}|g")
    if [[ "${STATICS_DB_CIDR:-}" == "none" ]]; then
        cidr_expr=(-e '/# >>> site:STATICS_DB_CIDR/,/# <<< site:STATICS_DB_CIDR/d')
    fi
    sed -e "s|image: \"\" *# IMAGE_PLACEHOLDER.*|image: ${IMAGE}|" \
        -e "s|IMAGE_PLACEHOLDER|${IMAGE}|g" \
        -e "s|BUILD_VERSION_PLACEHOLDER|${IMAGE_TAG}|g" \
        -e "s|SETTINGS_CHECKSUM_PLACEHOLDER|${SETTINGS_CHECKSUM}|g" \
        "${cidr_expr[@]}" "${manifest}"
}

apply_with_image() {
    render_manifest "$1" | kubectl -n "${NAMESPACE}" apply -f -
}

# Всё, что применяет деплой, в порядке применения. Используется --render;
# сам деплой ниже применяет те же файлы по шагам.
DEPLOY_MANIFESTS="k8s/base/rbac.yaml k8s/priorityclass.yaml ${SETTINGS_MANIFEST}
k8s/migrate-job.yaml k8s/executor.yaml k8s/base/deployment.yaml k8s/worker.yaml
k8s/networkpolicy.yaml k8s/squad-dashboard.yaml k8s/ci-deadman.yaml"

if [[ "${RENDER_ONLY}" == "1" ]]; then
    resolve_statics_cidr
    for m in ${DEPLOY_MANIFESTS}; do
        echo "---"
        echo "# Source: ${m}"
        render_manifest "${m}"
    done
    exit 0
fi

resolve_statics_cidr

# ── 1. Секреты ────────────────────────────────────────────────────────────
# НИКОГДА не применяем k8s/base/secrets.yaml: это шаблон с REPLACE_ME.
# Раньше скрипт делал `kubectl apply -f k8s/base/secrets.yaml` и ЗАТИРАЛ
# боевой Secret плейсхолдерами — мина, взрывающаяся при следующем рестарте
# pod-ов. Теперь только проверяем, что Secret-ы уже существуют
# (SealedSecrets / ExternalSecrets / secrets.local.yaml — см. k8s/base/).
#
# С 18.11.2026 их три — по ролям (минимальные привилегии): executor и beat
# больше не получают общий Secret целиком. Под, ссылающийся на отсутствующий
# Secret, висит в CreateContainerConfigError, поэтому проверяем до apply.
missing_secrets=""
for s in ${ROLE_SECRETS}; do
    if ! kubectl -n "${NAMESPACE}" get secret "${s}" >/dev/null 2>&1; then
        missing_secrets="${missing_secrets} ${s}"
    fi
done
if [[ -n "${missing_secrets}" ]]; then
    echo "ERROR: в ns ${NAMESPACE} нет Secret-ов:${missing_secrets}" >&2
    echo "  sre-ai-secrets — создать до деплоя (шаблон с именами ключей:" >&2
    echo "    k8s/base/secrets.app.yaml.example; SealedSecrets/ExternalSecrets — соседние *.example)." >&2
    echo "  sre-ai-executor-secrets / sre-ai-beat-secrets — собрать из него, не выводя значений:" >&2
    echo "    python3 scripts/split_role_secrets.py plan     # что будет скопировано" >&2
    echo "    python3 scripts/split_role_secrets.py apply" >&2
    exit 1
fi

# Настройка не должна жить в двух местах с разными значениями. Порядок envFrom
# — ConfigMap, затем Secret, то есть при дубле действует значение из Secret, а
# git показывает другое. Совпадающий дубль — предупреждение (убрать из Secret,
# RUNBOOK «Настройки: ConfigMap и секреты по ролям»); расходящийся — отказ до
# любых изменений в кластере. Печатаются только ИМЕНА ключей.
settings_pairs="$(kubectl create --dry-run=client -f "${SETTINGS_MANIFEST}" \
    -o go-template='{{range $k, $v := .data}}{{$k}}={{$v}}{{"\n"}}{{end}}')"
settings_conflicts=""
for s in ${ROLE_SECRETS}; do
    secret_keys="$(kubectl -n "${NAMESPACE}" get secret "${s}" \
        -o go-template='{{range $k, $v := .data}}{{$k}}{{"\n"}}{{end}}')"
    while IFS='=' read -r key want; do
        [[ -z "${key}" ]] && continue
        if ! grep -qx "${key}" <<<"${secret_keys}"; then
            continue
        fi
        # Значение читается только для ключа, который есть в ConfigMap, то
        # есть для несекретного флага; на экран не выводится.
        have="$(kubectl -n "${NAMESPACE}" get secret "${s}" \
            -o go-template="{{index .data \"${key}\" | base64decode}}")"
        have_norm="$(printf '%s' "${have}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
        want_norm="$(printf '%s' "${want}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
        if [[ "${have_norm}" == "${want_norm}" ]]; then
            echo "WARNING: ${key} дублируется в Secret ${s} (значение совпадает с ConfigMap) — убрать из Secret" >&2
        else
            settings_conflicts="${settings_conflicts} ${s}/${key}"
        fi
    done <<<"${settings_pairs}"
done
if [[ -n "${settings_conflicts}" ]]; then
    echo "ERROR: значения в Secret расходятся с ${SETTINGS_MANIFEST}:${settings_conflicts}" >&2
    echo "  Пока ключ лежит в Secret, действует значение из Secret, а не из git." >&2
    echo "  Сверить: python3 scripts/split_role_secrets.py verify" >&2
    echo "  затем привести ${SETTINGS_MANIFEST} к живому значению (или поменять его осознанно)." >&2
    exit 1
fi

# ── 2. RBAC ───────────────────────────────────────────────────────────────
# ServiceAccount sre-ai нужен до старта Job/Deployment.
kubectl -n "${NAMESPACE}" apply -f k8s/base/rbac.yaml
# PriorityClass кластерный: postgres/redis ссылаются на него (k8s/priorityclass.yaml).
kubectl apply -f k8s/priorityclass.yaml

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

# Обратный инвариант: у sre-ai (api, worker, beat) права на запись быть НЕ
# должно — её исполняет только copilot-executor под sre-ai-executor
# (k8s/executor.yaml). Лишний RoleBinding на sre-ai молча вернул бы
# write-токен api-поду, который принимает вебхуки из интернета; манифест
# этого не покажет, покажет только can-i.
#
# Проверяем по-настоящему, а не одним `can-i --all-namespaces`: тот отвечает
# лишь на вопрос о праве во ВСЕХ ns сразу и не видит RoleBinding в одном
# squad-foo — а write-роль именно так и выдаётся. Поэтому: cluster-wide плюс
# каждый ns, где есть RoleBinding с субъектом sre-ai. Имя субъекта — только
# фильтр кандидатов; решает can-i с реальной идентичностью SA.
#
# Нарушение = отказ деплоя, а не WARNING: выкатить api/worker, у которых
# остался write-токен, значит отрапортовать изоляцию, которой нет. Лечится
# переносом binding-а на sre-ai-executor (см. пример в k8s/base/rbac.yaml).
sre_ai_sa="system:serviceaccount:${NAMESPACE}:sre-ai"
write_leaks=""
if kubectl auth can-i patch deployments.apps --as="${sre_ai_sa}" \
        --all-namespaces >/dev/null 2>&1; then
    write_leaks="cluster-wide"
fi
while read -r rb_ns; do
    [[ -z "${rb_ns}" ]] && continue
    if kubectl auth can-i patch deployments.apps --as="${sre_ai_sa}" \
            -n "${rb_ns}" >/dev/null 2>&1; then
        write_leaks="${write_leaks} ${rb_ns}"
    fi
done < <(kubectl get rolebindings -A --no-headers \
            -o custom-columns='NS:.metadata.namespace,SUBJ:.subjects[*].name' \
            | awk '{n=split($2, s, ","); for (i=1; i<=n; i++) if (s[i]=="sre-ai") {print $1; break}}' \
            | sort -u)
if [[ -n "${write_leaks}" ]]; then
    echo "ERROR: SA sre-ai может patch deployments (${write_leaks# }) — запись должна быть только у sre-ai-executor." >&2
    echo "  Перенести binding-и: kubectl get rolebindings,clusterrolebindings -A -o wide | grep ' sre-ai\b'" >&2
    echo "  subject → sre-ai-executor (ns ${NAMESPACE}), затем повторить ./deploy.sh." >&2
    exit 1
fi

# Прямой инвариант — только предупреждение: executor включается флагами
# (k8s/base/settings.yaml), но писать может лишь туда, где есть RoleBinding на
# sre-ai-remediate (раздаёт scripts/grant_executor_squads.sh). Без них мост и кнопки
# «включены», а dry-run получает Forbidden. Выдавать запись — решение
# владельца кластера (пример binding-а — в k8s/base/rbac.yaml), деплой его
# не принимает и не блокируется.
executor_ns="$(kubectl get rolebindings -A --no-headers \
        -o custom-columns='NS:.metadata.namespace,ROLE:.roleRef.name,SUBJ:.subjects[*].name' 2>/dev/null \
    | awk '$2=="sre-ai-remediate" {n=split($3, s, ","); for (i=1; i<=n; i++) if (s[i]=="sre-ai-executor") {print $1; break}}' \
    | sort -u | tr '\n' ' ' || true)"
if [[ -z "${executor_ns// /}" ]]; then
    echo "WARNING: у SA sre-ai-executor нет ни одного RoleBinding на sre-ai-remediate —" >&2
    echo "  executor не может менять кластер ни в одном namespace (dry-run → Forbidden, кнопки Apply нет)." >&2
else
    echo "Executor может писать в namespace: ${executor_ns}"
fi

# ── 2б. Настройки: ConfigMap ДО миграций и приложения ────────────────────
# Job миграций и все пять Deployment-ов читают его через envFrom. Rollout при
# изменении даёт аннотация checksum/settings (см. render_manifest).
kubectl -n "${NAMESPACE}" apply -f "${SETTINGS_MANIFEST}"

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
    echo "    kubectl -n ${NAMESPACE} scale deploy copilot-worker copilot-worker-sync copilot-beat --replicas=0" >&2
fi

kubectl -n "${NAMESPACE}" delete job sre-ai-migrate --ignore-not-found
apply_with_image k8s/migrate-job.yaml
echo "Жду завершения миграций (job/sre-ai-migrate)..."
if ! kubectl -n "${NAMESPACE}" wait --for=condition=complete --timeout=300s job/sre-ai-migrate; then
    echo "ERROR: миграции не завершились. Логи:" >&2
    kubectl -n "${NAMESPACE}" logs "job/sre-ai-migrate" --tail=100 >&2 || true
    echo "" >&2
    echo "Если в логах lock_timeout — лок держит долгая транзакция. Заглуши писателей:" >&2
    echo "  kubectl -n ${NAMESPACE} scale deploy copilot-worker copilot-worker-sync copilot-beat --replicas=0" >&2
    echo "  ./deploy.sh ${NAMESPACE}   # повторить" >&2
    echo "  kubectl -n ${NAMESPACE} scale deploy copilot-worker --replicas=2" >&2
    echo "  kubectl -n ${NAMESPACE} scale deploy copilot-worker-sync copilot-beat --replicas=1" >&2
    exit 1
fi

# ── 4. Приложение ─────────────────────────────────────────────────────────
# executor — ПЕРВЫМ: api и worker выкатываются с EXECUTOR_DISPATCH=queue и
# сразу начинают класть dry-run/apply в его очередь. Поднятый позже, он
# стоил бы окна, где dry-run упирается в таймаут (без Apply — fail-closed,
# но зря).
apply_with_image k8s/executor.yaml
apply_with_image k8s/base/deployment.yaml
apply_with_image k8s/worker.yaml
# Через рендер, а не сырым apply: в файле плейсхолдер CIDR площадки.
apply_with_image k8s/networkpolicy.yaml

# ── 5. CronJob-ы на том же образе ────────────────────────────────────────
# До 18.11.2026 деплой их не трогал, а выкатка TeamCity (`kubectl set image`
# на пять Deployment-ов) о них не знает: squad-dashboard работал на 1.0.27,
# ci-deadman стоял на 1.0.0-rc.19 при приложении 1.0.35. Скрипт CronJob-а
# лежит в ConfigMap — обновляем его вместе с образом, иначе «образ свежий»
# ничего не значит.
#
# CronJob применяется, только если его окружение уже заведено (ConfigMap с env
# и Secret с токенами описаны в шапке манифеста и в git не хранятся): в
# кластере, где его нет, деплой приложения не должен ни падать, ни плодить
# Job-ы в CreateContainerConfigError.
apply_cronjob() {
    local name="$1" manifest="$2" script="$3"
    if ! kubectl -n "${NAMESPACE}" get configmap "${name}-env" >/dev/null 2>&1 \
        || ! kubectl -n "${NAMESPACE}" get secret "${name}-secrets" >/dev/null 2>&1; then
        echo "WARNING: CronJob ${name} пропущен — нет ConfigMap ${name}-env или Secret ${name}-secrets (см. шапку ${manifest})." >&2
        return 0
    fi
    kubectl -n "${NAMESPACE}" create configmap "${name}-script" \
        --from-file="$(basename "${script}")=${script}" \
        --dry-run=client -o yaml | kubectl -n "${NAMESPACE}" apply -f -
    apply_with_image "${manifest}"
}
apply_cronjob squad-dashboard k8s/squad-dashboard.yaml scripts/squad_dashboard.py
apply_cronjob ci-deadman k8s/ci-deadman.yaml scripts/ci_deadman.py

# Раньше здесь стояло `rollout status ... || true`, и финальное «Deploy OK»
# печаталось при ЛЮБОМ исходе: ImagePullBackOff, CrashLoopBackOff, зависший на
# readiness под — скрипт возвращал 0 и рапортовал успех. Тот же класс дефекта,
# что «зелёный билд при мёртвой статике»: единственный сигнал о выкате врёт, а
# CI/оператор верит exit code. Теперь неуспешный rollout = явный не-OK и exit 1.
#
# Цикл при этом НЕ выходит на первом упавшем (`if !`, а не голый вызов под
# set -e): оператору нужна полная картина по всем деплойментам, поэтому
# провалы копятся в rollout_failed, а exit — после цикла. `|| true` осталось
# только на диагностических kubectl-вызовах: их отказ не должен подменять
# причину падения (например, при отозванных правах на describe).
# 600s, а не 300s. Замер 21.08.2026: три выката подряд (rc.32, rc.33, rc.34)
# отчитывались «Deploy FAILED: sre-ai-api», хотя rollout доезжал через минуту
# после таймаута — то есть скрипт врал об исправном выкате. Причина в
# арифметике: при двух репликах maxUnavailable=25% округляется ВНИЗ до нуля,
# так что k8s обязан поднять новый под и дождаться его готовности прежде чем
# гасить старый, и обновление идёт строго последовательно. На пода уходит до
# 60s одного startupProbe (12 попыток × 5s), плюс терминация 30s.
#
# Ложное «FAILED» здесь не безобиднее ложного «OK»: оператор перестаёт верить
# и тому, и другому.
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-600s}"

rollout_failed=""
# copilot-worker-sync (очередь `sync`, k8s/worker.yaml) появился 31.10.2026.
# TC-деплой делает `kubectl set image` только по известным ему deployment-ам
# и новый не создаст: первый раскат — этим скриптом (apply_with_image
# k8s/worker.yaml выше), дальше `set image` подхватит его как остальные.
for d in copilot-executor sre-ai-api copilot-worker copilot-worker-sync copilot-beat; do
    if ! kubectl -n "${NAMESPACE}" rollout status "deploy/${d}" \
            --timeout="${ROLLOUT_TIMEOUT}"; then
        echo "ERROR: rollout deploy/${d} не сошёлся за ${ROLLOUT_TIMEOUT}." >&2
        # Причина почти всегда видна тут же: ImagePullBackOff (тег/registry),
        # CrashLoopBackOff (fail-closed валидаторы config.py на пустых секретах),
        # Unhealthy от liveness-пробы воркера (см. k8s/worker.yaml).
        kubectl -n "${NAMESPACE}" get pods -l "app=${d}" -o wide >&2 || true
        kubectl -n "${NAMESPACE}" describe deploy "${d}" 2>/dev/null \
            | tail -20 >&2 || true
        rollout_failed="${rollout_failed} ${d}"
    fi
done

if [[ -n "${rollout_failed}" ]]; then
    echo "" >&2
    echo "Deploy FAILED: не сошлись деплойменты:${rollout_failed}" >&2
    echo "  Образ ${IMAGE} и миграции применены — откат делать вручную:" >&2
    echo "    kubectl -n ${NAMESPACE} rollout undo deploy/<name>" >&2
    echo "    kubectl -n ${NAMESPACE} logs deploy/<name> --tail=100" >&2
    exit 1
fi

# CronJob-ы rollout-а не имеют; проверяем, что образ действительно сменился.
for cj in squad-dashboard ci-deadman; do
    cj_image="$(kubectl -n "${NAMESPACE}" get cronjob "${cj}" \
        -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].image}' 2>/dev/null || true)"
    if [[ -n "${cj_image}" && "${cj_image}" != "${IMAGE}" ]]; then
        echo "WARNING: CronJob ${cj} остался на образе ${cj_image}" >&2
    fi
done

echo "Deploy OK: ${IMAGE} → ns ${NAMESPACE}"
echo "(Отдельно применяются: k8s/vmalertmanagerconfig.yaml, k8s/monitoring.yaml,"
echo " k8s/postgres-backup.yaml; k8s/postgres.yaml и k8s/redis.yaml — только в окно:"
echo " правка шаблона StatefulSet перезапускает базу.)"
