# Semantic Contract

Документ фиксирует фактический контракт текущей реализации (v2.x), чтобы синхронизировать API, workers и эксплуатационные процессы.

## 1. Incident Lifecycle Contract
`IncidentRecord.status`:
- `PENDING` — инцидент принят API, поставлен в очередь.
- `COMPLETED` — пайплайн успешно завершен, результаты сохранены.
- `FAILED` — ошибка в процессе анализа/генерации.

Дополнительно для conversational pipeline используется state machine (`IncidentState`) с переходами вроде `INVESTIGATING -> HYPOTHESIS_GENERATED -> FIX_PROPOSED`.

## 2. Async Job Contract
Для endpoints, создающих фоновые задачи, ответ содержит:
- `task_id` — идентификатор Celery task.
- `status` или `location` — способ проверить прогресс.

Потребитель обязан поллить:
- `/webhooks/status/{task_id}` для webhook pipeline.
- `/jobs/{task_id}` для `/copilot` pipeline.

## 3. Feedback Contract
`POST /evaluation/{incident_id}/submit` принимает:
- `score: int`
- `is_accepted: bool`
- `comment?: str`

Агрегация на `GET /evaluation/stats` возвращает:
- `total_evaluated_incidents`
- `accepted_count`
- `accuracy_rate`

## 4. Execution Safety Contract
Любая операция remediation должна соответствовать двум слоям:
1. **DSL contract** (`ExecutionIntent`):
   - `action` из enum (`restart_deployment`, `scale_deployment`, `get_logs`, ...)
   - `resource_type` ограничен regex `(deployment|pod|service|ingress)`
   - системные namespace заблокированы валидатором
2. **Runtime policy** (`K8sSecurityGuard`):
   - whitelist `verb`/`resource`
   - blacklist namespace
   - deep inspection тела запроса (например, `privileged: true` запрещен)

## 5. Replay Mode Contract
Replay-сценарий запускает повторный анализ исторического инцидента и должен:
- не вызывать внешние side effects (например, Discord alerting),
- позволять гибкость переходов state machine при ретроспективном прогоне.
