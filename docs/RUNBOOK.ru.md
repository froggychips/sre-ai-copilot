# SRE AI Copilot — Runbook и история боевых прогонов

В этом документе зафиксированы сквозные тест-прогоны («боевые прогоны»), которые использовались для проверки и улучшения точности диагностического пайплайна. Для каждого прогона описаны: инжектируемый алерт, что наблюдал пайплайн, какая проблема была найдена, какой фикс был задеплоен и результат.

---

## Обзор пайплайна (краткая справка)

```
AlertManager webhook
  ↓
DiagnosticsEngine     ← правила → FactStore (oom_killed, process_crash, crashloop, …)
  ↓ fact_conflicts?  ← MUTUALLY_EXCLUSIVE_PAIRS → cap конфиденс + evidence.conflict_with
MultiHypothesisAgent  ← fan-out: app / infra / deps / runtime* (* precondition: process_crash)
  ↓ PERSPECTIVE_PRECONDITIONS фильтр
FactCriticAgent       ← adversarial grounding — каждая гипотеза vs FactStore
  ↓ выжившие
FixAgent              ← генерирует ExecutionIntent JSON
  ↓ is_recurrence? / jira_context?
RiskAgent             → Discord approval flow
  ↓
KnowledgeGraph        ← пишет только качественные причины (_is_quality_cause фильтр)
SimilarIncidentEngine ← детекция рецидивов (окно 7 дней)
```

---

## Прогон 1 — Smoke-тест (OOM false positive, базовый уровень)

**Дата:** 2026-05-12  
**Incident ID:** `e2e-smoke-4c30c313d831`  
**Алерт:** CrashLoopBackOff — под с exit code 139 (SIGSEGV)  
**Что инжектировалось:** webhook AlertManager с синтетическим SIGSEGV-подом.

### Что наблюдал пайплайн

```
facts_observed: ['oom_killed']
hypothesis_survivors: 0
cause: "No hypothesis survived adversarial critique. Observed facts: ['oom_killed']. Manual triage required."
resolution_quality: unresolved
```

### Найденная проблема

`OOMKilledRule` срабатывал по text-regex: находил строку "OOMKilled" в событиях подов пространства имён, хотя *целевой под* имел exit code 139 (не 137 = OOM). Ложноположительный факт `oom_killed` становился единственным наблюдаемым, и все гипотезы проваливали грандинг.

Дополнительно: "Manual triage required" записывалось в Knowledge Graph как `cause` при статусе RESOLVED, загрязняя будущую работу SimilarIncidentEngine.

### Задеплоенный фикс

| Элемент | Изменение |
|---|---|
| Структурный шлюз `OOMKilledRule` | Если целевой под имеет не-OOM exit code (≠ 0, ≠ 137) в `k8s_pod_state`, сразу вернуть `observed=False` — text-regex обход |
| KG quality gate | `_is_quality_cause()` отклоняет `None` и строки, начинающиеся с "No hypothesis survived…" или "Manual triage required" |
| `analysis["cause"] = None` | Когда ни одна гипотеза не выжила, сохранять `None`, а не строку ошибки |

---

## Прогон 2 — Smoke-тест (базовый уровень детекции конфликтов)

**Дата:** 2026-05-12  
**Incident ID:** `e2e-smoke-e36952331ba9`  
**Алерт:** CrashLoopBackOff, тот же SIGSEGV-сценарий после частичного патча text-regex.

### Что наблюдал пайплайн

```
facts_observed: ['oom_killed']
hypothesis_survivors: 0
cause: "No hypothesis survived adversarial critique. Observed facts: ['oom_killed']. Manual triage required."
```

### Найденная проблема

Даже после введения структурного шлюза text-regex путь мог срабатывать первым при определённых состояниях подов. Корневая причина: шлюз блокировал только при явно не-OOM `target_exit`; при отсутствии `k8s_pod_state` путь оставался открытым.

Также выявлено: если одновременно `oom_killed=True` и `process_crash=True`, FactCritic получал противоречивые данные и отсекал все гипотезы (pod либо умер от OOM, либо от SIGSEGV — не оба).

### Задеплоенный фикс

| Элемент | Изменение |
|---|---|
| `MUTUALLY_EXCLUSIVE_PAIRS` | `frozenset({oom_killed, process_crash})` — оба True = противоречие данных |
| `FactStore.conflicts()` | Перечисляет все активные конфликтующие пары |
| `_apply_conflict_signals()` | Снижает конфиденс конфликтующих фактов до 0.60; устанавливает `evidence.conflict_with` |
| Блок `<conflicts>` в промпте | `FactStore.to_prompt_context()` добавляет секцию `<conflicts>`, видимую всем агентам |

---

## Прогон 3 — Боевой SIGSEGV (false positive подтверждён)

**Дата:** 2026-05-12  
**Incident ID:** `run3-notificator-sigsegv`  
**Алерт:** `PodCrashLooping` — под `notificator`, namespace `squad-10-shared`, exit 139  
**Под:** реальный in-cluster под с 3 перезапусками

### Что наблюдал пайплайн

```
facts_observed: ['crashloop', 'oom_killed', 'process_crash']
fact_conflicts: [('oom_killed', 'process_crash')]  ← оба observed=True
hypothesis_survivors: 0
cause: "No hypothesis survived adversarial critique. Observed facts: ['crashloop', 'oom_killed', 'process_crash']. Manual triage required."
resolution_quality: unresolved
```

### Первопричина регрессии

Text-regex `OOMKilledRule` нашёл строку "OOMKilled" в событиях **других подов** того же namespace. Структурный шлюз ещё не был задеплоен. Результат:
- `oom_killed`: observed=True (false positive, conf=0.95)
- `process_crash`: observed=True (корректно, conf=0.97)

Система конфликт-кэпинга сработала, но блок `<conflicts>` ещё не был подключён к контексту FactCritic, поэтому критик отсёк все гипотезы.

### Задеплоенный фикс

Все элементы прогонов 1–2, плюс:

| Элемент | Изменение |
|---|---|
| `OOMKilledRule._check_pod_state()` | Сканирует все поды в `pod_state`, сначала целевой; возвращает структурированный Fact при первом найденном OOM-поде |
| Шлюз `target_exit` | Если целевой под имеет не-OOM exit code (≠ 0, ≠ 137) → `return Fact(observed=False)`, text-regex не достигается |
| `PERSPECTIVE_PRECONDITIONS` | `{"runtime": {FactKind.PROCESS_CRASH}}` — RuntimeAgent включается только при наблюдении `process_crash` |
| Детекция рецидивов | `SimilarIncidentEngine` детектирует resolved < 7 дней для того же сервиса → `recurrence=True` |
| FixAgent recurrence mode | `_RECURRENCE_PREFIX` заменяет митигацию investigative-инструкцией при `is_recurrence=True` |
| Jira enrichment | `JiraClient` запрашивает Atlassian REST API; `build_jira_context()` → `{open, resolved, has_open}` |

---

## Прогон 4 — Боевой SIGSEGV (все фиксы + Jira)

**Дата:** 2026-05-12  
**Incident ID:** `notificator-sigsegv-run4`  
**Алерт:** `PodCrashLooping` — тот же под `notificator`, exit 139  
**Jira:** Atlassian REST API v3 вернул `410 Gone` (устаревший GET /search → graceful degrade)

### Что наблюдал пайплайн

```
facts_observed: ['crashloop', 'process_crash']  ← oom_killed исчез!
fact_conflicts: []
hypothesis_survivors: 1
consensus_kinds: ['crashloop', 'process_crash']
cause: "Nil pointer dereference in startup initialization path"
resolution_quality: resolved
is_recurrence: False
jira_context: None  ← graceful degrade (Jira 410)
```

### Ключевые улучшения

| Метрика | Прогон 3 | Прогон 4 |
|---|---|---|
| `oom_killed` false positive | Да (conf=0.95) | Нет |
| `fact_conflicts` | `[oom_killed↔process_crash]` | `[]` |
| Выживших гипотез | 0 | 1 |
| Причина | "Manual triage required" | "Nil pointer dereference…" |
| resolution_quality | unresolved | **resolved** |
| KG загрязнён | Да | Нет |

### Дальнейшие действия

Jira REST API v3 `GET /search` вернул `410 Gone`. Atlassian перешёл на `POST /rest/api/3/search/jql`. `JiraClient` нужно обновить для использования POST-endpoint. Это низкоприоритетный фикс — пайплайн деградирует gracefully (без Jira-контекста FixAgent работает нормально).

---

## Известные ограничения / Следующие шаги

1. **Jira API endpoint**: `GET /rest/api/3/search` устарел; переключиться на `POST /rest/api/3/search/jql`.
2. **Верификация core dump**: `CoreDumpRule` даёт слабый сигнал без реального `ls -la` на хосте. Debug-под с hostPath mount дал бы точные размеры файлов и timestamps.
3. **TeamCity корреляция**: В прогоне 4 `teamcity_context=null` — TC MCP был недоступен в dev-окружении. В production контекст деплоя значительно обогащает анализ корневой причины.

---

## Executor-инциденты

Применимо к v0.7.0+ когда `EXECUTOR_ENABLED=true` и/или `EXECUTOR_APPROVAL_ENABLED=true`. С дефолтами (оба `false`) пайплайн чисто advisory и эта секция N/A.

### Как опознать executor-инцидент

Смотрим Discord embed:

| Режим | Сигналы в embed-е |
|---|---|
| Advisory only (default) | Нет «Dry-run verdict», нет Apply-кнопки. |
| Executor dry-run only   | «Dry-run verdict» (✓/✗/🚫/⚠️). Apply кнопки нет. |
| Executor + Apply         | Кнопка Apply присутствует если dry-run прошёл, risk ∈ {low, medium}, `executor_applied` не выставлен. |

DB-запрос:

```sql
SELECT
  incident_id,
  analysis -> 'execution_intent' AS intent,
  analysis -> 'executor_result'  AS dry_run_result,
  analysis -> 'executor_applied' AS applied
FROM incidents
WHERE incident_id = '<id>';
```

| Поле | Смысл |
|---|---|
| `execution_intent` | Структурное действие от `FixAgent` (`NULL` если LLM не выдал). |
| `executor_result.status` | Результат dry-run-а: `skipped` / `dry_run_ok` / `dry_run_failed` / `guardrail_blocked` / `error`. |
| `executor_applied` | Заполнен только после успешного Apply-клика. `NULL` = реального write-а не было. |

### Apply упал — что делать

#### kubectl вернул не-ноль

Симптом: ephemeral `❌ kubectl упал: …`, `executor_applied.result.success = false`, `stderr` содержит ошибку.

- `deployments.apps "xxx" not found` — устаревшее имя (ресурс удалили между dry-run и apply). State не изменился. Перетриггерить если нужно.
- `Operation cannot be fulfilled ... the object has been modified` — concurrent write. Безопасно retry-нуть.
- `timed out` — kubectl держался > 30с. Действие могло применится частично. Проверить: `kubectl get deployment <name> -n <ns> -o yaml | grep restartedAt`.

#### Guardrail заблокировал

Симптом: ephemeral `❌ Не могу применить: …`, audit `EXECUTOR_APPLY_REFUSED` с `reason="dry_run_not_ok:guardrail_blocked"`.

Это **ожидаемое поведение** — guard поймал policy violation (например, LLM предложил действие в `kube-*` или `prod-*` read-only tier). Никакого state change. Если кнопка Apply вообще появилась при таком state — баг.

#### executor_applied есть, но действие было неверным

Откат руками:

```bash
# rollout restart — откатить на предыдущий ReplicaSet:
kubectl rollout undo deployment/<name> -n <ns>

# scale — вернуть прежнее число реплик:
kubectl scale deployment/<name> -n <ns> --replicas=<original>
```

Пометить инцидент как ошибочный (KG quality gate исключит из similar-incident lookup-ов):

```sql
UPDATE incidents
   SET analysis = jsonb_set(analysis, '{resolution_quality}', '"wrong_apply"')
 WHERE incident_id = '<id>';
```

Кликнуть 👎 на embed чтобы зафиксировать negative feedback структурно.

### Killing executor

#### Остановить новые applies (нормальный control)

```bash
helm upgrade sre-ai-copilot helm/sre-ai-copilot \
  --reuse-values \
  --set env.EXECUTOR_APPROVAL_ENABLED=false
```

Apply-кнопка исчезает с новых embed-ов. Уже выставленные embed-ы остаются в Discord history, но клик возвращает `❌ EXECUTOR_APPROVAL_ENABLED=false — apply отключён.`

#### Emergency (быстрее всего)

```bash
kubectl scale deployment/sre-ai-copilot-worker -n sre-ai --replicas=0
```

Останавливает всё (dry-run, analysis, apply). Только если executor сильно misbehaviour.

#### Permanent

Поставить и `EXECUTOR_ENABLED=false`, и `EXECUTOR_APPROVAL_ENABLED=false`. Пайплайн опускается до advisory.

### Audit trail

OTEL root-span атрибуты:
- `sre.incident.execution_intent_parsed: bool`
- `sre.incident.execution_intent_action`
- `sre.incident.executor_status`

Span стадии `executor` испускает event `guardrail.blocked` при отказе K8sSecurityGuard.

Audit log events (фильтр по `event`):

| Event | Когда |
|---|---|
| `K8S_GUARDRAIL_BLOCK` | Guard отказал при execute_intent |
| `K8S_COMMAND_ATTEMPT` | Перед запуском kubectl |
| `K8S_COMMAND_RESULT` | kubectl завершился |
| `K8S_COMMAND_TIMEOUT` | kubectl > 30с |
| `K8S_BLOCKED_NO_APPROVAL` | `dry_run=False` вызван без `post_approval=True` |
| `EXECUTOR_APPLIED` | Apply успешно |
| `EXECUTOR_APPLY_REFUSED` | Apply ineligible |
| `EXECUTOR_APPLY_EXCEPTION` | Неожиданная exception в apply-service |
| `EXECUTOR_DRY_RUN_FAILED` | Exception в `stage_executor` |

---

## KG self-health alerts

Beat-задача `kg_self_health_check` запускает шесть canary'ев каждые 30 минут. На любой `fail`/`warn` шлёт single embed в `DISCORD_WEBHOOK_SELF_HEALTH_URL` (отдельный канал от `#infra-error` — намеренно, чтобы операционные алерты и tooling-алерты не конкурировали).

Audit-log event'ы: `KG_SELF_HEALTH_OK` / `KG_SELF_HEALTH_WARN` / `KG_SELF_HEALTH_FAIL`. Фильтр по `check_name` чтобы детализировать.

### `materialization_zero_rate` fail на конкретной метрике (например, `cpu_pct`)

Что значит: больше допустимого % строк `kg_service_health` за 24 ч имеют значение 0 или NULL для этой метрики.

Allowlist: `http_5xx_rate` и `p95_latency_ms` ожидаемо ноль, пока ingress scrape config не настроен — на них этот canary НЕ должен срабатывать. Если срабатывает — allowlist в `self_health.py` устарел.

Диагностика по порядку:

1. Доступна ли VictoriaMetrics? `curl <vm-host>/api/v1/query?query=up`. Если нет — VM лежит; canary справедливо орёт, чинить VM.
2. PromQL всё ещё валиден? Открыть `metrics_sync.py`, скопировать запрос в VM UI напрямую — он что-то возвращает на заведомо живом сервисе?
3. Менялась ли форма запроса недавно? См. Wave 5 gotcha: aggregate-then-divide молча отдаёт 0. Корректная форма для ratio-метрик — divide-then-aggregate.
4. Жив ли celery-worker, который крутит `kg_metrics_sync`? `celery -A app.celery_worker inspect active`.

### `sync_lag` fail на `kg_seq_logs_sync` (или любой другой sync)

Что значит: последний write timestamp для этого источника старше 5× ожидаемого интервала.

Типичные причины по sync'у:
- `kg_seq_logs_sync` — Seq недоступен, API key протух, egress заблокирован.
- `kg_metrics_sync` — VM недоступна.
- `kg_topology_sync` — kubeconfig невалиден или k8s API throttled.
- `tc_deploys_to_kg` — TC MCP недоступен или token протух.

Generic check: tail логов celery-worker, grep по имени задачи; искать exceptions или repeated retries.

### `anomaly_signal_health: 0 observations`

Что значит: anomaly-детектор написал ноль строк за последние 24 ч.

Две интерпретации:
- **Flat baseline** — исходная метрика вся нули (например, ingress без scrape). Ожидаемо; не проблема.
- **Detector regression** — детектор отработал, но output не выдал, хотя в источнике есть variance. Посмотреть variance в `kg_service_health`, потом запустить `anomaly_detection.py` интерактивно на известно-аномальном сервисе, проверить поле `extras.method`.

### `anomaly_signal_health: >500 observations`

Что значит: пороги слишком слабые, либо добавили новую метрику с природным шумом.

Поднять `KG_ANOMALY_ROBUST_Z_WARN` / `_CRIT` вверх или сузить baseline window. Volume guard кэпит на 3/час на (service, metric), так что >500 за 24 ч означает распыление по многим сервисам — скорее всего global threshold issue, а не один горячий сервис.

### `pod_events_link_rate <50%`

Что значит: больше половины `kg_pod_events` за последние 24 ч имеют `service_id IS NULL`.

Это StS-резолвер регрессия. Pod naming для StS использует ordinal suffixes (`-0`, `-1`) вместо deployment hash; проверить, что в `k8s_events_sync.py` живой cascading fallback Deployment regex → StatefulSet regex → DaemonSet regex.

### `edges_freshness >30% stale`

Что значит: слишком много `kg_service_edges` имеют `last_seen_at` старше 24 ч или NULL.

Самая вероятная причина: `kg_topology_sync` не запускается, не refresh'ит `last_seen_at` или upsert'ит под не той identity. Проверить celery beat logs на hourly run, и убедиться в БД что `max(last_seen_at)` сдвигается с каждым прогоном.

---

## Setting up Discord approvers

Approve / Decline на incident-embed'ах гейтятся. Без сконфигурированных approver'ов система fail-closed — кнопки отказывают с audit `DISCORD_APPROVAL_DENIED_NO_APPROVERS_CONFIGURED`.

### Получить Discord user ID

В Discord-клиенте включить Developer Mode (Settings → Advanced → Developer Mode). Дальше right-click по нужному юзеру → "Copy ID". ID — числовая строка 17–19 цифр.

### Конфигурация

```bash
# Allowlist по юзерам (CSV):
DISCORD_APPROVERS_USER_IDS="123456789012345678,234567890123456789"

# Либо по ролям (разрешает любого, у кого есть хотя бы одна из этих ролей):
DISCORD_APPROVERS_ROLE_IDS="345678901234567890"

# Опционально override квоты (default 5/час на юзера):
DISCORD_APPROVAL_RATE_LIMIT_PER_HOUR="10"
```

Списки юзеров и ролей объединяются (user-id match OR role match → allowed).

### Fail-closed семантика

Если оба списка `DISCORD_APPROVERS_USER_IDS` и `DISCORD_APPROVERS_ROLE_IDS` пустые, каждый клик отказывает с `DISCORD_APPROVAL_DENIED_NO_APPROVERS_CONFIGURED`. Никакого "default allow" — это by design.

### Тестирование

1. Прописать env-vars, рестартнуть API + worker.
2. Послать тестовый incident; убедиться что в embed'е есть кнопки Approve / Decline.
3. Из-под allowlisted юзера: клик Approve → ephemeral "approved" + строка в audit log.
4. Из-под не-allowlisted юзера: клик Approve → ephemeral deny + audit `DISCORD_APPROVAL_DENIED_UNAUTHORIZED`. Проверить что отказ НЕ потребил rate-limit квоту легитимного approver'а.

---

## Per-team Discord channels

Discord-рендерер умеет роутить incident'ы и дайджесты в разные каналы по `team_owner`. Конфигурируется через `DISCORD_TEAM_CHANNEL_MAP` как JSON-строка:

```json
{
  "platform": "https://discord.com/api/webhooks/.../platform-channel",
  "payments": "https://discord.com/api/webhooks/.../payments-channel",
  "gameplay": "https://discord.com/api/webhooks/.../gameplay-channel"
}
```

- Ключи матчатся с `kg_services.team_owner` (выводится из namespace prefix).
- Несовпавшие команды откатываются на `DISCORD_WEBHOOK_URL`.
- Добавить новую команду: дописать запись; код менять не надо. Рестартнуть worker, чтобы подхватил.

---

## Signal quality tuning

Anomaly-пороги тюнятся через env-vars. Robust-z статистически well-defined (3.5 ≈ p99.95 на нормально-подобном распределении), но реальные метрики имеют тяжёлые хвосты.

### Конфигурация

```bash
KG_ANOMALY_ROBUST_Z_WARN="3.5"   # default — производит "warning" строки
KG_ANOMALY_ROBUST_Z_CRIT="6.0"   # default — производит "critical" строки
```

### Симптомы false-positive overload

- Canary `anomaly_signal_health` переходит в `warn` с `>500 obs/24h`.
- Discord pipeline начинает агрессивно дедупить сам себя (одна и та же аномалия каждые 30 мин).
- Операторы жалуются на «шум» в embed'ах.

### Как тюнить

1. **Поднять пороги** — `KG_ANOMALY_ROBUST_Z_WARN` до 4.5 или 5.0. Грубый, но быстрый фикс.
2. **Сузить baseline window** — по умолчанию детектор использует 7 дней. Если у метрики недельные циклы отличаются от дневных, seasonal baseline становится шумным. Сужение окна помогает при коротких циклах.
3. **Проверить `flat_baseline`** — если у многих аномалий `extras.flat_baseline=true`, метрика реально мёртвая, и детектор срабатывает на переходе "0 → 1". Обычно это корректно, но стоит проверить, что метрика должна быть живой.

### Симптомы false-negative

- Canary `anomaly_signal_health` переходит в `warn` с `0 obs/24h`.
- Известные инциденты не поднимают deploy correlator signal, хотя метрики явно прыгнули.

Понизить пороги или проверить, что seasonal-стратификация не маскирует сигнал (нужно ≥50 исторических точек — у недавно появившихся сервисов их ещё нет).

---

## Deploy correlator verdict

Каждый инцидент, прошедший enrichment, получает блок `deploy_correlator` в `analysis`. Verdict tier управляет тем, что покажет Discord embed.

### Что значит verdict

| Verdict | Confidence | Действие |
|---|---|---|
| `likely` | ≥ 0.7 | Отрисовывается как блок "Suspect deploy" в embed'е с TC-ссылкой + автором. Смотреть деплой первым. |
| `suspect` | 0.4 – 0.7 | Отрисовывается мягче ("possibly related"). Смотреть после primary hypothesis. |
| `weak` | 0.2 – 0.4 | Остаётся в `analysis` JSON, в embed не идёт. Полезно для backfill / audit. |
| `unlikely` | < 0.2 | Пишется для полноты, дальше игнорируется. |

### Посмотреть confidence напрямую

```sql
SELECT
  incident_id,
  analysis -> 'deploy_correlator' -> 'verdict'    AS verdict,
  analysis -> 'deploy_correlator' -> 'confidence' AS confidence,
  analysis -> 'deploy_correlator' -> 'factors'    AS factors,
  analysis -> 'deploy_correlator' -> 'deploy'     AS deploy
FROM incidents
WHERE incident_id = '<id>';
```

`factors` показывает multi-factor разбивку:

- `n_spikes` — anomaly count после деплоя в окне
- `max_zscore` — пиковый robust-z
- `time_proximity` — ближе по времени = выше
- `deploy_status_factor` — FAILED > SUCCESS
- `flat_baseline_penalty` — снижает confidence на спайках мёртвой метрики

### Когда копать руками

- `likely`, но деплой был docs-only → проверить `deploy_status_factor` и `time_proximity`; деплой мог совпасть по времени с независимым инцидентом. Пометить инцидент `resolution_quality = 'unresolved'`, чтобы не засорять KG.
- `suspect`, а интуиция оператора говорит "точно он" → посмотреть на `max_zscore`. Если он чуть ниже warning-порога, исходная метрика может быть пограничной по шуму.
- `weak`, но интуиция говорит "yes" → проверить, не вылез ли деплой за 2-часовое окно (incident lag из-за VM scrape interval может сдвинуть timestamp позже, чем реально).
