#!/bin/bash
# Права записи для copilot-executor в namespace стендов.
#
# Без аргументов — только показывает план. --apply — применяет. --revoke — снимает.
# Создаёт RoleBinding sre-ai-remediate → SA sre-ai-executor в каждом namespace,
# подходящем под TARGET_NS_RE, со ссылкой на ClusterRole sre-ai-remediate
# (k8s/base/rbac.yaml). ClusterRoleBinding не создаётся никогда: запись выдаётся
# по одному namespace. Namespace, созданный ПОСЛЕ запуска, привязки не имеет —
# скрипт идемпотентен, его можно перезапускать.
#
# bash 3.2 (macOS). Переменные: TARGET_NS_RE, SA_NS, CHECK_DENY_NS.
set -u
MODE="${1:-plan}"
SA_NS="${SA_NS:-sre-ai}"
SA=sre-ai-executor
NAME=sre-ai-remediate
RE="${TARGET_NS_RE:-^squad-[0-9]+-(shared|kingdom[0-9]+)\$}"
DENY="${CHECK_DENY_NS:-prod-shared preupdate-shared kube-system}"

NSLIST=$(kubectl get ns -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | grep -E "$RE" | sort)
COUNT=$(printf '%s\n' "$NSLIST" | grep -c .)
if [ "$COUNT" -eq 0 ]; then echo "Не нашёл ни одного namespace стенда — kubectl вернул пусто, повтори."; exit 1; fi

cluster_role() {
cat <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: $NAME
  labels: {app.kubernetes.io/part-of: sre-ai-copilot}
rules:
- apiGroups: ["apps"]
  resources: ["deployments", "deployments/scale"]
  verbs: ["get", "list", "patch", "update"]
EOF
}

binding() {
cat <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: $NAME
  namespace: $1
  labels: {app.kubernetes.io/part-of: sre-ai-copilot}
subjects:
- kind: ServiceAccount
  name: $SA
  namespace: $SA_NS
roleRef:
  kind: ClusterRole
  name: $NAME
  apiGroup: rbac.authorization.k8s.io
EOF
}

check() {
  first=$(printf '%s\n' "$NSLIST" | head -1)
  for ns in "$first" "$SA_NS" $DENY; do
    printf '  patch deployments в %-20s → %s\n' "$ns" \
      "$(kubectl auth can-i patch deployments -n "$ns" --as="system:serviceaccount:$SA_NS:$SA" 2>&1)"
  done
}

case "$MODE" in
  plan)
    echo "ПЛАН (ничего не меняю). Стендов-namespace: $COUNT"
    printf '%s\n' "$NSLIST" | sed 's/^/  /' | head -6; echo "  …"
    echo "Будет создано: ClusterRole $NAME (только описание прав, без привязки ко всему кластеру)"
    echo "              + по одному RoleBinding $NAME в каждом из $COUNT namespace → SA $SA_NS/$SA"
    echo "Сейчас:"; check
    echo "Применить: $0 --apply    Снять: $0 --revoke"
    ;;
  --apply)
    cluster_role | kubectl apply -f - || exit 1
    ok=0; fail=0
    for ns in $NSLIST; do
      if binding "$ns" | kubectl apply -f - >/dev/null 2>&1; then ok=$((ok + 1)); else fail=$((fail + 1)); echo "  НЕ применилось: $ns"; fi
    done
    echo "RoleBinding применён в $ok namespace, ошибок: $fail"
    echo "Проверка:"; check
    # Частичная выдача — не успех: часть стендов осталась без права записи.
    [ "$fail" -eq 0 ] || exit 1
    ;;
  --revoke)
    n=0
    for ns in $(kubectl get rolebinding -A -o jsonpath='{range .items[?(@.metadata.name=="'$NAME'")]}{.metadata.namespace}{"\n"}{end}'); do
      kubectl -n "$ns" delete rolebinding "$NAME" >/dev/null 2>&1 && n=$((n + 1))
    done
    kubectl delete clusterrole "$NAME" --ignore-not-found
    echo "Снято RoleBinding: $n"; echo "Проверка:"; check
    ;;
  *) echo "Использование: $0 [--apply|--revoke]"; exit 2 ;;
esac
