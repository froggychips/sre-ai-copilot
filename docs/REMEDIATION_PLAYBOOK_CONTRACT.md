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
