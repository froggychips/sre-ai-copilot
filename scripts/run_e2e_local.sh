#!/usr/bin/env bash
# Локальный e2e для sre-ai-copilot БЕЗ Anthropic API key.
#
# Архитектура запуска:
#   - LLM:           Claude Code CLI (`claude --print`) — через ClaudeCliService
#   - Pipeline:      PIPELINE_DIRECT_INVOKE=true → инлайн в FastAPI route
#                    (минуя Celery/Redis, так как eager-Celery несовместим с async)
#   - DB:            SQLite-файл локально (declarative create_all)
#   - Discord:       DISCORD_DRY_RUN=true (только logging)
#   - TC контекст:   mcp-teamcity-server из MR !1, поднят в подпроцессе на :8101
#
# Запуск:
#   bash scripts/run_e2e_local.sh
# Опции:
#   E2E_KEEP_RUNNING=1   — не убивать uvicorn/mcp-teamcity по выходу
#   E2E_NAMESPACE=...    — namespace в синтетическом alert-е (default preprod-shared)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_DIR"

# --- ENV для FastAPI/Celery/наших сервисов ---
export ENV=development
export ANTHROPIC_API_KEY=__not_used__           # pydantic Settings требует «непустое», бэкенд игнорирует
export DISCORD_WEBHOOK_URL=__dry_run__
export DISCORD_DRY_RUN=true
export DATABASE_URL="sqlite:///$REPO_DIR/e2e-local.db"
export REDIS_URL=redis://127.0.0.1:6379/0       # не используется при PIPELINE_DIRECT_INVOKE
export LLM_BACKEND=claude_cli
export MODEL_NAME=haiku                          # 6 stages × ~3-5s через CLI
export PIPELINE_DIRECT_INVOKE=true
export CELERY_TASK_ALWAYS_EAGER=false           # не нужен при PIPELINE_DIRECT_INVOKE
export TEAMCITY_MCP_URL=http://127.0.0.1:8101/mcp
export TEAMCITY_WEB_URL=https://wo-teamcity.lastoasisgame.com
export TC_LOOKBACK_MINUTES=10080                # неделя — гарантированно что-нибудь найдём
export SAFE_MODE=true
export APPROVAL_REQUIRED=false
export LOG_LEVEL=INFO
NAMESPACE="${E2E_NAMESPACE:-preprod-shared}"

API_PORT=8888

# --- pid storage + teardown ---
PIDS=()
cleanup() {
    if [ "${E2E_KEEP_RUNNING:-0}" = "1" ]; then
        echo "(keeping bg processes alive — PIDs: ${PIDS[*]:-})"
        return
    fi
    for pid in "${PIDS[@]:-}"; do
        kill "$pid" 2>/dev/null || true
    done
}
trap cleanup EXIT

# --- 1. init SQLite-схему ---
echo "[1/5] init SQLite ($DATABASE_URL)"
"$REPO_DIR/.venv/bin/python" -c "
from app.database import Base, engine
Base.metadata.create_all(engine)
print('  tables:', list(Base.metadata.tables.keys()))
"

# --- 2. mcp-teamcity-server локально на :8101 ---
TC_TOKEN_VAL=$("$REPO_DIR/.venv/bin/python" -c "
import json,sys
try:
    print(json.load(open('${HOME}/.claude.json'))['mcpServers']['teamcity']['env']['TC_TOKEN'])
except Exception as e:
    print('', file=sys.stderr); sys.exit(0)
")
TC_MCP_SRC="/tmp/external-mcp/teamcity-server"
if [ -n "$TC_TOKEN_VAL" ] && [ -d "$TC_MCP_SRC" ]; then
    echo "[2/5] starting mcp-teamcity-server on :8101"
    (
        cd "$TC_MCP_SRC/src"
        TEAMCITY_URL=https://wo-teamcity.lastoasisgame.com \
        TEAMCITY_TOKEN="$TC_TOKEN_VAL" \
        MCP_HOST=127.0.0.1 MCP_PORT=8101 \
        "$TC_MCP_SRC/.venv/bin/python" -m server
    ) >/tmp/mcp-tc-e2e.log 2>&1 &
    PIDS+=($!)
    # дожидаемся, пока слушает
    for i in 1 2 3 4 5 6 7 8 9 10; do
        if nc -z 127.0.0.1 8101 2>/dev/null; then break; fi
        sleep 0.5
    done
else
    echo "[2/5] (skip mcp-teamcity: no token or source missing — TC context будет пустым)"
fi

# --- 3. uvicorn ---
echo "[3/5] starting uvicorn on :$API_PORT"
"$REPO_DIR/.venv/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT" >/tmp/uvicorn-e2e.log 2>&1 &
PIDS+=($!)
for i in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:$API_PORT/healthz" >/dev/null 2>&1 \
       || curl -fsS -X POST "http://127.0.0.1:$API_PORT/webhooks/alertmanager" -H "Content-Type: application/json" -d 'X' 2>/dev/null | grep -q .; then
        break
    fi
    sleep 0.5
done
echo "  uvicorn ready"

# --- 4. синтетический AlertManager-webhook ---
NOW=$(date -u +%FT%TZ)
FP="e2e-test-$(date +%s)"
PAYLOAD=$("$REPO_DIR/.venv/bin/python" -c "
import json,sys
print(json.dumps({
  'id':'$FP',
  'version':'4','groupKey':'e2e-test/manual','status':'firing',
  'receiver':'sre-copilot','groupLabels':{'alertname':'BackendServiceHighLatency'},
  'commonLabels':{},'commonAnnotations':{},
  'externalURL':'https://alertmanager.local',
  'alerts':[{
    'status':'firing',
    'labels':{
      'alertname':'BackendServiceHighLatency','severity':'warning',
      'namespace':'$NAMESPACE','pod':'town-service-7c8d-xyz','service':'town-service'
    },
    'annotations':{
      'summary':'town-service p99 latency above 800ms for 5m',
      'description':'p99 latency 1230ms (threshold 800ms) on town-service in $NAMESPACE. CrashLoopBackOff на 1 поде. CPU norm.'
    },
    'startsAt':'$NOW','endsAt':None,
    'generatorURL':'https://prometheus.local/graph?expr=town_latency_p99',
    'fingerprint':'$FP'
  }]
}))
")

echo "[4/5] POST /webhooks/alertmanager (ns=$NAMESPACE, fp=$FP) — это занимает 1–5 минут (6 LLM-вызовов через claude CLI)"
HTTP_RESPONSE=$(echo "$PAYLOAD" | curl -sS -X POST "http://127.0.0.1:$API_PORT/webhooks/alertmanager" \
    -H "Content-Type: application/json" --data @- --max-time 900)
echo "  $HTTP_RESPONSE"

# --- 5. результаты ---
echo ""
echo "[5/5] результат из БД:"
"$REPO_DIR/.venv/bin/python" -c "
import json, sqlite3, sys
con = sqlite3.connect('$REPO_DIR/e2e-local.db')
con.row_factory = sqlite3.Row
row = con.execute('SELECT * FROM incidents WHERE incident_id=?', ('$FP',)).fetchone()
if not row:
    print('  NO RECORD'); sys.exit(1)
print('  status:', row['status'])
print('  created_at:', row['created_at'])
data = json.loads(row['data']) if row['data'] else {}
print('  summary:', data.get('summary'))
print('  namespace:', data.get('namespace'))
tcx = data.get('teamcity_context')
if tcx:
    print(f'  teamcity_context: branch={tcx[\"branch\"]} builds={len(tcx[\"recent_builds\"])}')
    for b in tcx['recent_builds'][:2]:
        print(f'    - #{b[\"number\"]} {b[\"status\"]} {b[\"buildtype_id\"]} ({b[\"branch\"]})')
else:
    print('  teamcity_context: (none)')
trace = json.loads(row['trace']) if row['trace'] else []
print(f'  trace ({len(trace)} stages):')
for t in trace:
    print(f'    {t[\"stage\"]:<10} {t[\"duration_ms\"]:>6} ms  {len(t.get(\"llm_calls\", []))} LLM-call(s)')
analysis = json.loads(row['analysis']) if row['analysis'] else {}
syn = analysis.get('synthesis')
if syn:
    print(f'  synthesis (first 600 chars):')
    for line in syn[:600].splitlines():
        print(f'    {line}')
    if len(syn) > 600:
        print(f'    ... [+{len(syn)-600} chars]')
"

echo ""
echo "logs: /tmp/uvicorn-e2e.log, /tmp/mcp-tc-e2e.log"
