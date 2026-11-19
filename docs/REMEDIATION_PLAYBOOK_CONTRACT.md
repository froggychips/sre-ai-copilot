# Контракт исполнимого playbook-а

Что гарантирует `restart_crashloop_deployment` и `scale_out_cpu_throttled_deployment`
на пути от алерта до записи в кластер. Каждая строка таблиц ниже проверяется тестом в
`tests/remediation/test_playbook_contracts.py`; golden-кейсы 021–024 проверяют
совпадение и несовпадение на реплее.

Путь действия, слои идут строго по порядку:

```
preconditions → policy → привязка к снимку → gate → пере-dry-run → попытка → verify
```

Отказ на любом слое окончателен: следующий слой не запускается, и в кластер ничего не
пишется. Флаг `REMEDIATION_PLAYBOOK_BINDING_ENABLED` выключен по умолчанию. Пока он
выключен, gate не читает снимок, а верхние слои работают как preview.

## 1. Preconditions (matcher)

| Условие | Выполнено | Не выполнено |
|---|---|---|
| `found` | есть FOUND-факт этого kind | ABSENT, UNKNOWN, факта нет |
| `absent` | только ABSENT-факты | FOUND, UNKNOWN, факта нет, конфликт FOUND+ABSENT, конфликт ABSENT+UNKNOWN |
| `evidence_not_in` | у каждого FOUND-факта есть ключ, и значение не из списка | ключа нет, значение из списка |

До preconditions matcher сверяет вид цели с `match.target_kinds` (необязательное поле
v2; допустимые значения — `Deployment`, `StatefulSet`, `DaemonSet`, `Job`, `CronJob`,
`Cluster`):

| `match.target_kinds` | Вид цели | Итог |
|---|---|---|
| не задан | любой, неизвестный | вид не проверяется |
| задан | входит в список | дальше — preconditions |
| задан | не входит в список | не совпал (`target_kind_mismatch`) |
| задан | не определён | не совпал (`target_kind_unknown`, fail-closed) |

Вызов `match_playbooks` без `target_kind` (pipeline до выбора цели, golden-реплей) вид
не проверяет. Оба playbook-а реестра объявляют `target_kinds: ["Deployment"]`.

- **restart:** `crashloop` found; `oom_killed`, `recent_deploy` absent; `process_crash`
  found без падения по сигналу (exit-коды 132, 134, 135, 136, 138, 139).
- **scale:** `resource_pressure` found; `crashloop`, `oom_killed`, `recent_deploy` absent.

## 2. Policy

`auto` нет ни у одного из двух: лучший исход — одобрение человеком.

| namespace / цель | решение |
|---|---|
| `squad-*`, `dev-*` | approve |
| `prod-*`, `preprod-*` | block |
| системные ns (`monitoring`, `sre-ai`, …) и неизвестный tier | block |
| цель похожа на data-plane по имени (`*-postgres`, `*-redis`, …) | block |

## 3. Привязка к серверному снимку (`remediation.binding`)

Снимок пишет сервер в момент отбора, модель к нему доступа не имеет. Hash записи снимка
(`binding`) входит в подпись intent-а, так что одобрение покрывает ровно эту запись.

| отказ | когда |
|---|---|
| `match_snapshot_missing` | снимка нет или версия чужая |
| `binding_missing` | у intent-а нет hash-а |
| `playbook_not_matched` | в снимке нет записи этого playbook-а |
| `snapshot_tampered` | запись правили после отбора (hash не сходится с содержимым) |
| `binding_mismatch` | hash intent-а от другого снимка (другой инцидент, re-fire) |
| `namespace_mismatch` | ns intent-а ≠ ns снимка |
| `playbook_changed_since_match` | YAML поменялся после отбора (новый образ) |
| `server_param_missing` | план ждёт `current_replicas`, а сервер его не снял |
| `server_param_mismatch` | `current_replicas` или цель intent-а ≠ снимку |
| `intent_not_in_plan` | действие, тип или параметры — не шаг плана |

Как понимаются параметры плана:

- шаблоном считается только строка целиком `{name}`, по тому же паттерну, что у рендера;
- литералы сравниваются после валидаторов `ExecutionIntent`, поэтому `"2"` и `2` — одно значение;
- лишний параметр в intent-е означает несовпадение с шагом.

## 4. Scale и конкурентный скейл

`current_replicas` — серверный параметр:
- модель его не пишет, значение из вывода LLM отбрасывается;
- после выбора цели pipeline снимает живое `spec.replicas`, кладёт его в
  `server_params` записи снимка и пересчитывает hash;
- команда уходит как `kubectl scale --current-replicas=N`.

Если между одобрением и кликом Deployment отскейлили (HPA, оператор, человек), пере-dry-run
падает у apiserver, и запись не выполняется. Если живое значение снять не удалось, срабатывает
`server_param_missing`: scale без precondition не исполняется.

Схема не пропустит в план шаг с `ConcurrencyPolicy.PRECONDITION` без
`current_replicas: "{current_replicas}"`, а серверный параметр, записанный литералом,
отклоняется у любого действия.

## 5. Попытка

- Запись снимка, под которую одобрен intent, сохраняется в строке
  `kg_remediation_attempts` (`intent.bound_playbook_entry`).
- Повтор по тому же инциденту отсекается.
- Протухший план (`intent_stale`) отклоняется до gate.

## 6. Verify

Список обязательных проверок берётся только из записи с hash-ом, равным hash-у
intent-а. Порядок поиска: сначала запись из попытки, затем запись из
`analysis.playbook_match`.

- Если re-fire заменил снимок, а у попытки своей записи нет (исполнено до этого поля),
  исход `binding_lost`. Редакция реестра при этом не подставляется.
- `binding_lost` никогда не даёт verified: на промежуточных попытках это pending, на
  последней unknown.
- Playbook только ужесточает исход `assess()`: failed и unknown остаются как есть.
- Проверка вернула False: на промежуточной попытке pending, на последней failed.
- Проверку не удалось выполнить (None): pending, на последней unknown.

## 7. Детерминированный мост (без LLM)

`app/remediation/bridge.py`, флаг `REMEDIATION_BRIDGE_ENABLED`. Второй, кроме
FixAgent, вход в этот контракт — прямо с прод-пути алертов (`/store`,
`/enrich-and-forward`):

- API только ставит задачу: `app/remediation/bridge_dispatch.py` проверяет
  `wants_bridge` (флаг + `alertname` из `match.alertnames` исполнимого playbook-а),
  `REMEDIATION_BRIDGE_NAMESPACES` (префиксы; пусто — все) и дедуп постановки
  (Redis `SET NX EX`, `REMEDIATION_BRIDGE_ENQUEUE_DEDUP_SECONDS`), затем
  `apply_async` задачи `remediation_bridge` в очередь `celery` (с 1.0.32). Redis
  недоступен — задача не ставится. Вся работа ниже идёт в воркере;

- вид и имя цели определяются до любого kubectl (`app/remediation/workload.py`,
  флаг `REMEDIATION_BRIDGE_TARGET_KIND_ENABLED`): явный лейбл алерта (`deployment`,
  `statefulset`, `daemonset`, `job_name`, `cronjob`) → workload-узел графа с именем,
  выведенным из пода (`metadata_json.k8s_workload.workload_kind`), или Job из
  `kg_k8s_jobs` → форма имени пода (`<имя>-<rs-hash>-<pod-hash>` — Deployment,
  `<имя>-<цифры>-<pod-hash>` — Job от CronJob, `<имя>-<ordinal>` — StatefulSet) → вид
  неизвестен. Имя Deployment-а берётся оттуда же, а не из имени сервиса обогащения;
- цель, вид которой определён и не подходит ни одному playbook-у с этим `alertname`,
  отсекается в начале задачи — до обогащения и снимка K8sFacts; остальные несовпадения
  по виду отсекает `match_playbooks`. В обоих случаях — debug-строка
  `remediation_bridge.playbook_skipped` и счётчик
  `remediation_bridge_skipped_total{reason,playbook,target_kind}` (причины
  `target_kind_mismatch`, `target_kind_unknown`, `plan_render_failed`);
- блок policy по осям, не зависящим от живого состояния (`namespace_tier`,
  `resource_kind`, `data_plane`, `reversibility`), выносится до пробы живых реплик
  (флаг `REMEDIATION_BRIDGE_EARLY_POLICY_BLOCK_ENABLED`): gate считается на заглушках
  реплик, и решение принимается, только если это `block` с причиной по одной из этих
  осей. Решение пишется в `kg_remediation_decisions` как раньше; `command_preview` у
  такой строки без чисел реплик, счётчик — `remediation_bridge_early_blocks_total`;
- preconditions считаются по фактам правил `enrich_alert` и по фактам
  состояния пода (K8sFacts → `OOMKilledRule`, `ProcessCrashRule`,
  `PodEventsRule`); снимок не снят — `absent` понижается до `unknown`;
- intent — первый мутирующий шаг `plan.steps`, собранный `render_plan`; снимок
  и привязка — `build_match_snapshot` + `bind_server_params`, как у pipeline.
  Мост подставляет в план `namespace`, имя цели и серверные шаблоны
  (`_plan_context`): плану scale-out мост ставит `current_replicas` = живое `spec.replicas` и `replicas` = на одну больше (реплик нет или ноль — предложения нет); `resource_pressure` берётся из самого горящего `CPUThrottlingHigh`, а `crashloop: absent` — только из успешного снимка, где целевой под не нездоров (с 1.0.34). Другие шаблоны мост не заполняет — `plan_render_failed`,
  предложения нет. v1-playbook-и (`cleanup_stale_failed_job`, только
  `match.classification`) мост не берёт;
- gate зовётся с `require_binding=True` и при отборе, и на apply-пути
  (`analysis.source == "remediation_bridge"`), независимо от
  `REMEDIATION_PLAYBOOK_BINDING_ENABLED`;
- `approve` → карточка с кнопками в канал (сквадам — узкое исключение из
  «в канал не постим»), не чаще раза в 6 ч на (playbook, uid, incarnation);
  `block` → строка в обычной карточке, без кнопок.

