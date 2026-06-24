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

Allowlist: `http_5xx_rate` и `p95_latency_ms` по-прежнему ожидаемо ноль — application `/metrics` сидят за JWT и отдают 401 scraper-у; фикс требует изменений в бэкенде — тикет WO-12483. На них этот canary НЕ должен срабатывать; если срабатывает — allowlist в `self_health.py` устарел. Пока WO-12483 не закрыт, `health_score` — инфра-прокси (без app-level HTTP-сигнала). Внимание: к ingress-level HTTP-метрикам это больше не относится — `kg_ingress_observations` наполняется (см. «Поток ingress-метрик» ниже).

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
- `kg_ingress_observations_sync` — VM недоступна, `VMPodScrape`-объекты `ingress-nginx-shared` / `ingress-prod` (ns `cattle-system`) пропали, либо на controller DaemonSet выключен per-host metrics (см. «Поток ingress-метрик» ниже).

Generic check: tail логов celery-worker, grep по имени задачи; искать exceptions или repeated retries.

### Поток ingress-метрик (kg_ingress_observations)

Текущее состояние (2026-06-10): метрики nginx-ingress включены на **обоих** контроллерах кластера WO (`--enable-metrics=true`; per-host лейблы обязаны оставаться включёнными — sync фильтрует по лейблу `host`). Скрейп идёт через `VMPodScrape`-объекты в ns `cattle-system` с `honorLabels: true`. Beat-задача `kg_ingress_observations_sync` запускается каждые ~10 минут и пишет per-host/path строки p95/p99/rps/4xx/5xx в `kg_ingress_observations`; 100% строк слинкованы с `kg_services`. `error_5xx_rate = 0` при ненулевом `rps` теперь реально означает «ошибок нет», а не «нет данных».

Если `kg_ingress_observations` перестала наполняться (или опустела), диагностика по порядку:

1. Живы ли scrape-объекты? `kubectl -n cattle-system get vmpodscrape ingress-nginx-shared ingress-prod`. Если какого-то нет — поток метрик в VM мёртв.
2. Не выключен ли metrics-per-host на controller DaemonSet-ах? Его выключение убивает лейбл `host` — а с ним весь sync, хотя контроллер по-прежнему экспортирует метрики.
3. Виден ли поток в VictoriaMetrics? Запрос `count(nginx_ingress_controller_requests) by (namespace)` к `vmsingle-vm-victoria-metrics-k8s-stack.monitoring.svc:8428`. Ноль / нет series → проблема со скрейпом; ненулевые значения → дыра на стороне копилота.
4. Если в VM данные есть, а таблица не наполняется — tail логов celery worker/beat, искать exceptions у `kg_ingress_observations_sync`.

### `anomaly_signal_health: 0 observations`

Что значит: anomaly-детектор написал ноль строк за последние 24 ч.

Две интерпретации:
- **Flat baseline** — исходная метрика вся нули (например, `kg_service_health.http_5xx_rate` / `p95_latency_ms`, которые останутся 0 до закрытия WO-12483). Ожидаемо; не проблема. Внимание: ingress-метрики сюда больше не подходят — `kg_ingress_observations` наполняется с 2026-06; если ingress-источник стал flat, сперва проверять скрейп (см. «Поток ingress-метрик» выше).
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

## Render-time подавление шума (MUTE) — kill-switch'и

Два render-time класса подавления (добавлены в v0.14.0) держат хронически-
шумные формы алёртов **видимыми, но тихими**: карточка всё равно постится —
grey + 🔇, **без** бейджа 🚨 и без `@mention`. Это **MUTE**, не DROP и не
demote severity: инцидент по-прежнему сохраняется, embed на месте для глазной
проверки — убирается только громкость.

Они в том же render-time ряду, что и `rollout_noise`
(`ROLLOUT_SUPPRESS_ENABLED`, окно `ROLLOUT_SUPPRESS_WINDOW_MINUTES`=15 —
подавление в окне деплоя). Отличать все три от **input-level DROP**
`ALERT_SUPPRESS_NAMES` (Watchdog / InfoInhibitor / KubeAPIServerSlo), где
карточка вообще не появляется.

| Класс | Env (default) | Тег | Что приглушает |
|---|---|---|---|
| `meta_noise` | `META_NOISE_ENABLED` (true) | 🔇 META-AGGREGATE | Всегда-шумные мета-агрегаты: `*NewCriticalAlerts` (Prod/Preprod/Squad) + control-plane scrape-gap `etcdInsufficientMembers` / `ScrapePoolHasNoTargets` / `RecordingRulesNoData`. Каждый реальный критикал и так приходит отдельной громкой карточкой, агрегат-счётчик ничего не добавляет. |
| `gen_mismatch_noise` | `GEN_MISMATCH_NOISE_ENABLED` (true) | 🔇 GENERATION-CHURN | **Условный** churn `KubeDeploymentGenerationMismatch`. Приглушаем **только** при здоровых репликах (`ready==desired`, `≥1`). При `ready<desired` / `?/N` / `None` / `0/0` → **fail-safe LOUD** — реально зависший накат всё равно звенит в любом namespace, включая prod. |

### Временно вернуть громкость классу (kill-switch)

⚠️ Эти флаги пока **не** проброшены в Helm-чарт — шаблоны деплойментов мапят
лишь фиксированный набор (`executorEnabled`, `safeMode`, `llmBackend`, …)
через `.Values.env.*`. Поэтому `helm upgrade --set env.META_NOISE_ENABLED=false`
**ничего не делает**: чарт это значение не потребляет, приложение остаётся на
дефолте из config.py (`true`). Флаг читается из env на **старте** процесса
(не hot-reload), так что любая смена требует рестарта подов.

Два способа отключить класс:

**1. Быстро / эфемерно — `kubectl set env`.** Выставить на **обоих**
деплойментах: render (enrichment + сборка embed) идёт в worker, а api держит
inline-путь при `PIPELINE_DIRECT_INVOKE=true`.

```bash
kubectl set env -n sre-ai \
  deployment/sre-ai-copilot-api deployment/sre-ai-copilot-worker \
  META_NOISE_ENABLED=false      # или GEN_MISMATCH_NOISE_ENABLED=false
```

`kubectl set env` сам триггерит rollout (ручной рестарт не нужен). Этот
override **затирается следующим `helm upgrade`** — чарт его не отслеживает.
Использовать для «сделать громким прямо сейчас, пока разбираюсь».

**2. Durable — изменение чарта.** Добавить env-проброс в **оба** шаблона
`deployment-api.yaml` и `deployment-worker.yaml` + дефолт в `values.yaml`
(camelCase-ключ, как `executorEnabled`):

```yaml
# templates/deployment-{api,worker}.yaml — в блок env:
- name: META_NOISE_ENABLED
  value: {{ .Values.env.metaNoiseEnabled | default "true" | quote }}

# values.yaml — под env:
#   metaNoiseEnabled: "true"
#   genMismatchNoiseEnabled: "true"
```

Затем `helm upgrade` — теперь `--set env.metaNoiseEnabled=false` реально
сработает и переживёт последующие upgrade-ы.

В обоих случаях приглушённый класс возвращается к обычному severity-routing
(громко 🚨 / `@mention` на critical). Вернуть назад — флипнуть значение
обратно в `true`.

### Диагностика «почему алёрт пришёл серым с 🔇 и без пинга?»

1. **Смотрим alertname.** Это мета-агрегат (`*NewCriticalAlerts`) или
   scrape-gap (`etcdInsufficientMembers` / `ScrapePoolHasNoTargets` /
   `RecordingRulesNoData`)? Тогда это `meta_noise` — by design. Реальный
   критикал под ним, если он есть, приходит отдельной громкой карточкой.
2. **Для `KubeDeploymentGenerationMismatch`** — смотрим поле реплик в
   карточке (`replicas_ready_desired`):
   - `ready==desired` → приглушён намеренно (`gen_mismatch_noise`). Это
     churn от внешнего контроллера (Rancher дописывает аннотацию
     `publicEndpoints` → бьёт `metadata.generation`, хотя накат давно
     сошёлся).
   - карточка показывает деградацию (`ready<desired` / `?/N` / `0/0`),
     **а всё равно тихо** → это **баг** в fail-safe-пути. Проверить
     детектор и значение `replicas_ready_desired`, которое получил embed —
     он должен был остаться LOUD.

Прецеденты: `meta_noise` — ProdNewCriticalAlerts 2026-06-16 (PR #154);
`gen_mismatch_noise` — prod-kingdom7/town-service 2026-06-23 (PR #160).

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

---

## KG quality_report — baseline snapshot

CLI `quality_report` — канонический способ снять baseline KG-quality
перед/после крупной remediation-волны, чтобы видеть delta, а не угадывать.

### Запуск

```bash
# Markdown в stdout (default):
python -m app.scripts.quality_report

# JSON в stdout (для diff / dashboard ingestion):
python -m app.scripts.quality_report --json

# Сохранить snapshot в файл:
python -m app.scripts.quality_report --markdown --output baseline.md
```

Скрипт **read-only** — никаких INSERT/UPDATE/DELETE. Безопасно гонять
на production. Использует ту же `SessionLocal` что и production-copilot,
поэтому credentials БД подхватываются автоматически.

### Что считает

Пять секций:

1. **Services** — total / real / synthetic / orphans / by `stale_class` /
   owner coverage.
2. **Edges** — по `kind` (calls / uses_nats / uses_db / serves_traffic /
   routes_to / uses_volume / bound_to) / freshness / multi-source ratio.
3. **Events** — deploys по статусу / pod_events linkage rate (с
   `service_id`) / alerts open vs resolved.
4. **Coverage** — Jobs/CronJobs / Storage Volumes / NATS subjects.
5. **Quality flags** — известные data-quality проблемы с anchor-ами
   (например, "12 unowned ns с deploys за 30d — suspect owner_inference gap").

### Baseline на v0.12.0

`docs/quality_report_baseline_2026_05_24.md` — снимок сразу после
мерджа Wave 8. Использовать как anchor для Phase A remediation.

---

## Ownership manifest

Multi-signal owner inference (`ownership_suggester.suggest_owner_multi_signal`)
пробует три эвристики параллельно (prefix / deploy-history / labels).
Когда ни одна не подходит или ответ неверный — override через YAML manifest.

### Setup

```bash
# Путь к YAML (должен быть читаем worker-pod-у):
OWNERSHIP_MANIFEST_PATH=/etc/sre-ai/ownership.yaml
```

### YAML формат

```yaml
# Каждая запись: ns_pattern (glob), owner (строка как есть в digest),
# reason (свободная форма, идёт в audit log). Опциональный name_pattern —
# переопределить один сервис внутри общего ns.
- ns_pattern: "ml-*"
  owner: "@ml-platform"
  reason: "ML infra пока без labels — owner подтверждён в Slack 2026-05-23"

- ns_pattern: "vendor-acme"
  owner: "@vendor-acme"
  reason: "Third-party namespace без internal owner — эскалация через partnership"

- ns_pattern: "*-backup"
  owner: "@platform"
  reason: "Все backup CronJob-ы platform-owned по policy"

# Per-service override внутри multi-tenant ns:
- ns_pattern: "*-shared"
  name_pattern: "clickhouse*"
  owner: "@data"
  reason: "Analytics-стек owned дата-командой"
```

- `ns_pattern` — Python `fnmatch` (glob, не regex).
- `name_pattern` (опц.) — сужает правило до конкретного сервиса в ns.
  Применяется только при per-service вызове
  `suggest_owner_multi_signal(ns, db, name=svc.name)` (так зовёт
  `app/scripts/backfill_ownership.py`). Digest-level callers передают
  `name=None`, и тогда правила с `name_pattern` пропускаются —
  работают только ns-level правила.
- Первый match побеждает — порядок важен; **специфичные правила
  (с `name_pattern`) кладите выше catch-all-а по ns**.
- Match в manifest даёт `confidence=1.0` и оверрайдит все три эвристики.

### Bundled manifest для `*-shared` инфраструктуры

В репо лежит `config/ownership.yaml` покрывающий 132 сервиса в
`preprod-shared` / `preupdate-shared` / `prod-shared` / `squad-gd-shared`,
которым multi-signal heuristics не могут найти owner-а (никакой squad
ими не владеет одним). Категоризация:

- ClickHouse (analytics) → `@data`
- NATS / message bus → `@platform`
- PostgreSQL replicas / backups / metrics → `@platform`
- VictoriaMetrics / kube-state-metrics → `@platform`
- Seq logging, update-service, config-workers → `@platform`
- `squad-gd-shared` app services (auth, push, mv, …) → `@squad-gd`

Подмонтируйте через configmap и укажите `OWNERSHIP_MANIFEST_PATH` на
файл.

### Активация `*-shared` ownership manifest в runtime

В master уже лежит и manifest (`config/ownership.yaml`), и Helm-обвязка:
`templates/ownership-configmap.yaml` рендерит ConfigMap из
`helm/sre-ai-copilot/files/ownership.yaml` (синхронизированная копия —
`.Files.Get` в Helm не выходит за пределы chart-dir-а), worker/api
deployments монтируют его в `OWNERSHIP_MANIFEST_PATH`
(дефолт `/config/ownership.yaml`).

CI gate `tests/test_helm_ownership_sync.py` проверяет, что
`config/ownership.yaml` ≡ `helm/sre-ai-copilot/files/ownership.yaml`
байт-в-байт. Если правите один — синкайте другой:

```bash
cp config/ownership.yaml helm/sre-ai-copilot/files/ownership.yaml
```

#### A. Через Helm (рекомендуется)

```bash
# values.yaml дефолтно ownershipManifest.enabled=true.
# Поправили helm/sre-ai-copilot/files/ownership.yaml (новое правило):
helm upgrade --install sre-ai-copilot helm/sre-ai-copilot/ \
  --namespace sre-ai \
  -f helm/sre-ai-copilot/values.yaml \
  -f your-overrides.yaml

# Прокатить worker/api чтобы подцепили configmap:
kubectl -n sre-ai rollout restart deploy/sre-ai-copilot-worker
kubectl -n sre-ai rollout restart deploy/sre-ai-copilot-api

# Проверка:
kubectl -n sre-ai exec deploy/sre-ai-copilot-worker -- \
  sh -c 'echo $OWNERSHIP_MANIFEST_PATH; head -5 $OWNERSHIP_MANIFEST_PATH'
# Ожидаем: /config/ownership.yaml + первые 5 строк manifest-а.
```

#### B. Manual configmap (без Helm)

```bash
kubectl -n sre-ai create configmap sre-ai-copilot-ownership \
  --from-file=ownership.yaml=config/ownership.yaml \
  --dry-run=client -o yaml | kubectl apply -f -

# Затем patch deployments — mount /config/ownership.yaml (subPath
# ownership.yaml, readOnly) + env OWNERSHIP_MANIFEST_PATH=/config/ownership.yaml.
# Точная форма — в templates/deployment-worker.yaml и deployment-api.yaml.
```

#### Backfill уже attribute-нутых сервисов

После того как configmap живой — перегнать inference на `*-shared`,
чтобы старые attributions сошлись к manifest-вердиктам (confidence=1.0
всегда выигрывает):

```bash
kubectl -n sre-ai exec deploy/sre-ai-copilot-worker -- \
  python -m app.scripts.backfill_ownership --apply --filter-ns '*-shared'
```

Периодическая beat-таска `kg-ownership-backfill`
(`OWNERSHIP_BACKFILL_ENABLED=true`) тоже перепрогоняет каждые 6h, но
явный one-shot быстрее после смены manifest-а.

#### Выключить

`helm upgrade ... --set ownershipManifest.enabled=false` снимает
ConfigMap, env-var и mount — worker откатывается на три heuristics
(prefix / deploy-history / labels), 132 `*-shared` сервиса снова
становятся unowned.

### Добавить / изменить override

1. Отредактировать `config/ownership.yaml` — добавить правило. Правила
   с `name_pattern` кладите **выше** generic ns catch-all-а.
2. Локально прогнать `pytest tests/test_shared_ownership_manifest.py -x`.
3. Открыть PR. После merge helm/configmap rollout подхватит изменения.
4. Перепривязать уже attribute-нутые сервисы при необходимости:

   ```bash
   kubectl -n sre-ai exec deployment/copilot-worker -- \
     python -m app.scripts.backfill_ownership --apply \
     --filter-ns '*-shared'
   ```

   `--filter-ns` принимает glob. Manifest matches применяются всегда
   (confidence=1.0 ≥ любой threshold).

### Reload

Manifest перечитывается на каждом `suggest_owner_multi_signal`-вызове,
но путь файла кэшируется по environment. Чтобы сменить manifest — поменять
env-var и рестартнуть worker.

### Audit

Каждый match emit-ит audit-log line `KG_OWNER_MANUAL_OVERRIDE` с
`ns_pattern`, `owner`, `reason`. Используется для верификации, что
manifest реально читается в production.

### Alias map для deploy-history сигнала

Для эвристики deploy-history (сигнал B) TC usernames транслируются в
team-handles через `app/services/owner_aliases.py`. Override:

```bash
OWNER_ALIASES_PATH=/etc/sre-ai/owner-aliases.yaml
```

YAML формат:

```yaml
kemyashev: "@squad-1"
apleshkov: "@squad-2"
wizaryx:   "@platform"
new-engineer: "@squad-N"
```

Pre-baked defaults в коде; YAML расширяет/оверрайдит. Ключи lowercase.

---

## Ownership backfill (`app.scripts.backfill_ownership`)

`suggest_owner_multi_signal` подвязан в `unowned_namespaces`-секцию
digest-а, но **периодический `kg_topology_sync` его не зовёт** —
сервисы без owner-а на initial discovery так и остаются `owner=NULL`
forever, пока что-то не пнёт. Скрипт `backfill_ownership` закрывает
эту дыру: пробегает по всем `kg_services` где `team_owner IS NULL`,
гоняет multi-signal inference и пишет результат, если confidence
прошёл threshold.

Заодно бэкфилит `stale_class` для строк с `stale_class IS NULL` через
тот же классификатор что у `kg_sync` (см. `--stale` / `--all`).

### Когда запускать

- **После initial deploy / свежего restore.** Topology sync знает
  только что говорят k8s-labels; backfill подтягивает сигналы
  deploy-history + prefix, которым нужна история.
- **Когда `owner_known < 80%`** в дневном digest или
  `kg_quality_report` (секция "KG quality_report" выше). Стагнация
  обычно означает, что сервисы создавались до того, как сигналы
  inference стали работать.
- **Еженедельно.** Когда beat-таска включена (следующая секция) —
  периодический цикл делает это автоматом; ручной прогон нужен только
  для первого rollout-а или когда хочется one-off catch-up с
  пониженным threshold.

### Dry-run preview (default — безопасно, без записи)

```bash
kubectl -n sre-ai exec deployment/sre-ai-api -- \
  python -m app.scripts.backfill_ownership --dry-run --threshold 0.5
```

Печатает `total_candidates_owner`, `would_update_owner`,
`skipped_low_confidence` и `kept_existing` — без записи в БД. По
этой цифре решаем — стоит ли apply.

### Пониженный threshold для понимания gap-а

```bash
kubectl -n sre-ai exec deployment/sre-ai-api -- \
  python -m app.scripts.backfill_ownership --dry-run --threshold 0.3
```

Threshold `0.3` показывает prefix-only suggestions (confidence ≈ 0.4),
которые `0.5` режет. Полезно понять **почему** gap всё ещё висит:
если `would_update_owner` подскакивает с ~50 на `0.5` до ~2000 на
`0.3` — фикс не "поднять threshold у beat", а "настроить
`OWNERSHIP_MANIFEST_PATH`, чтобы prefix не был единственным сигналом".

### Apply (production-запись)

```bash
kubectl -n sre-ai exec deployment/sre-ai-api -- \
  python -m app.scripts.backfill_ownership --apply --threshold 0.4
```

Threshold `0.4` — рекомендуемый initial-rollout: включает prefix-матчи,
но не голые single-source guess-ы на `0.3`. Идемпотентно — повторный
прогон на тех же данных no-op (фильтр по `team_owner IS NULL`).

**Реальный пример (2026-05-24):** прод-кластер стоял на
`owner_known = 12.40%` (335 / 2702 сервисов). Один прогон
`--apply --threshold 0.4` поднял до `owner_known = 86.68%`
(+74 pp, 1994 сервиса забэкфилены с `namespace_prefix` как primary
source). Оставшиеся ~13% — реально без owner-а: third-party namespaces,
ad-hoc CronJob-ы — это закрывается через `OWNERSHIP_MANIFEST_PATH`.

### Бэкфил `stale_class` тоже

```bash
# Только stale_class:
kubectl -n sre-ai exec deployment/sre-ai-api -- \
  python -m app.scripts.backfill_ownership --stale --apply

# Оба ownership + stale_class за один прогон:
kubectl -n sre-ai exec deployment/sre-ai-api -- \
  python -m app.scripts.backfill_ownership --all --apply --threshold 0.4
```

### Откат

Скрипт пишет только в строки где `team_owner WAS NULL` (фильтр в
`plan_ownership`). Также проставляет `metadata_json.owner_source`
тем сигналом, что победил: `namespace_prefix`, `k8s_labels`,
`deploy_history` (и `manual` для `OWNERSHIP_MANIFEST_PATH`-матчей).
Откатить прогон без удара по pre-existing / manual-overriden owner-ам:

```sql
UPDATE kg_services
   SET team_owner = NULL
 WHERE metadata_json->>'owner_source'
       IN ('namespace_prefix','deploy_history','k8s_labels');
```

`owner_source = 'manual'` и строки куда backfill не писал
(`owner_source IS NULL`, pre-existing owners) — не трогаем. После SQL
следующий sync оставит сервисы `owner=NULL` пока не прогонится
очередной backfill.

---

## Периодический ownership backfill (beat-таска `kg-ownership-backfill`)

После того как initial ручной `--apply`-rollout прошёл хорошо, переводим
процесс из "ops запускает руками" в "Celery beat прогоняет сам" — та же
entry-point `run_backfill`, частота 6 ч, scope — только high-confidence
сигналы.

### Конфигурация

| Env var | Default | Назначение |
|---|---|---|
| `OWNERSHIP_BACKFILL_ENABLED` | `false` | Master switch. Beat-таска no-op-ит когда off. |
| `OWNERSHIP_BACKFILL_THRESHOLD` | `0.7` | Минимум confidence для периодической записи. |

В Helm — через `values.yaml`:

```yaml
env:
  ownershipBackfillEnabled: "true"
  ownershipBackfillThreshold: "0.7"
```

Оба прокидываются в worker Deployment (см.
`helm/sre-ai-copilot/templates/deployment-worker.yaml`).

### Логика порогов: `0.4` initial vs `0.7` periodic

- **Initial rollout (ручной, `0.4`):** ops смотрит результат, поэтому
  ОК включать prefix-only матчи, которым нужен human plausibility check.
  Большой one-time win (тот самый +74 pp выше).
- **Periodic beat (`0.7`):** гоняется без присмотра каждые 6 ч. Высокий
  порог = commit только при multi-signal agreement (prefix + labels,
  или deploy_history совпал с prefix). Новые сервисы, не прошедшие
  bar, остаются `owner=NULL` и всплывают в дневном
  `unowned_namespaces`-digest-е, где человек или добавит запись в
  `OWNERSHIP_MANIFEST_PATH`, или следующий sync даст достаточно
  сигнала.

Не опускать beat-порог до `0.5` и ниже "чтобы закрыть gap" — это
сносит слой human-review и будет перерисовывать owner-ов каждые 6 ч
при флипе single-signal-а. Лучше — one-off ручной
`--apply --threshold 0.5` или расширить ownership manifest.

### Beat schedule + observability

- Schedule: `crontab(minute=17, hour="*/6")` — каждые 6 ч, offset от
  drift/ingress/stuck-sync-ов, чтобы избежать DB-contention.
- Имя таски: `kg_ownership_backfill` (Celery flower / логи).
- Log line на каждый прогон: `kg_ownership_backfill.done updated=N
  skipped_low_conf=M kept=K`. `updated=0` подряд много прогонов —
  ожидаемый steady state; non-zero — только когда приходят новые сервисы.

---

## stale_class на kg_services

Wave 8 вводит `kg_services.stale_class` (PR #86). Три значения:

| Значение | Смысл |
|---|---|
| `active` | Deploy за последние `ACTIVE_WINDOW_DAYS` (default 30d). |
| `expected_stale` | Не катился 30d, но это норма: backup/cron/system имена, infra/platform-owned ns. |
| `suspicious_stale` | Нет deploys 30d, не подходит под expected-паттерны. |

Column переписывается **идемпотентно** через `kg_sync.sync_namespace` на
каждом sync (hourly). Stats_digest читает column как primary с fallback
на legacy in-memory classifier для инсталляций без свежего sync.

### Реклассифицировать сервис

Если сервис misclassified (например, `expected_stale` который реально
должен катиться чаще), есть три рычага:

1. **Переименовать**: убрать suffix `-backup` / `-cron` / `-job`, который
   pattern-match'ит в `expected_stale`. Следующий sync переклассифицирует.
2. **Сменить namespace**: переехать в не-`expected_stale` namespace
   (`kube-system`, `monitoring` в system-списке).
3. **Сменить owner**: `team_owner = platform` триггерит `expected_stale`
   при отсутствии recent deploys. Поставить squad-owner.

Manual override через SQL **не рекомендуется** — column перепишется на
следующем sync. Это derived-поле, не authoritative.

### Запросы

```sql
-- Все suspicious_stale сервисы с последним деплоем:
SELECT
  s.namespace, s.name, s.team_owner,
  s.stale_class,
  MAX(d.started_at) AS last_deploy
FROM kg_services s
LEFT JOIN kg_deployments d ON d.service_id = s.id
WHERE s.stale_class = 'suspicious_stale'
  AND NOT s.synthetic
GROUP BY s.id
ORDER BY last_deploy NULLS FIRST;
```

Запрос в production для поиска кандидатов на retire/handoff.

---

## Discord snapshot fixtures (UX regression-guard)

Wave 8-G (PR #88) добавляет gallery из 7 snapshot-cases для Discord
embed-ов — любое UX-изменение в `app/services/discord/embed_builder.py`
или связанных модулях должно обновить snapshot-ы, иначе CI ляжет.

### Cases

`tests/fixtures/discord_snapshots/`:

1. `01_critical_fresh` — first-fire critical alert с полным enrichment.
2. `02_critical_resurfaced` — тот же alert, возвращающийся после resolve.
3. `03_warning_compact` — warning severity, без enrichment.
4. `04_burst_aggregation` — тот же alert N раз в dedup-окне.
5. `05_daily_digest` — daily stats digest (KG-summary).
6. `06_chronic_digest` — chronic-suppressed alerts visibility digest.
7. `07_team_digest` — per-team fragile services digest.

Каждый case имеет `input.json` (payload алерта/инцидента) и `expected.json`
(отрисованный embed). Раннер — `tests/test_discord_alert_gallery.py`.

### Update workflow

При намеренном изменении embed-UX:

```bash
# Перерисовать и перезаписать все expected.json:
UPDATE_SNAPSHOTS=1 pytest tests/test_discord_alert_gallery.py

# Просмотреть diff:
git diff tests/fixtures/discord_snapshots/

# Закоммитить если изменения корректные:
git add tests/fixtures/discord_snapshots/
git commit -m "UX: обновить snapshot-фикстуры после <change>"
```

### Просмотр snapshot-diff-ов

Раннер pretty-print-ит diff в pytest output при провале фикстур. Смотрим:

- **Title / description text** — самая user-visible регрессия.
- **Field order** — изменения здесь меняют scanability embed-а.
- **Color / severity badge** — visual регрессия с одного взгляда.
- **Footer / timestamp** — обычно шум; игнорировать если ts-логика не менялась.

Если diff слишком большой для глазного review — гонять с `-vv` для full
side-by-side, или открыть `input.json` и перерисовать в изоляции.

### Добавить новый case

1. Создать `XX_new_case.input.json` с минимальным payload alert/incident.
2. `UPDATE_SNAPSHOTS=1 pytest tests/test_discord_alert_gallery.py -k new_case`.
3. Проверить сгенерированный `XX_new_case.expected.json` — выглядит корректно?
4. Закоммитить оба файла вместе.

Цель: каждая embed-форма (severity × enrichment-state × digest-type)
должна иметь хотя бы один case, чтобы ловить регрессии.
