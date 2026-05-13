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
