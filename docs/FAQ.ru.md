# sre-ai-copilot — FAQ

Практические ответы по развёртыванию и настройке AI-помощника SRE.

---

## Что это делает?

AlertManager отправляет webhook → копилот получает алерт → многоагентный LLM-пайплайн (Analyzer → Hypothesis → Critic → Fix → Risk) выдаёт анализ и, если нужно, `ExecutionIntent` → намерение уходит в Discord на аппрув → ты одобряешь → действие выполняется в Kubernetes.

Также есть `/copilot` — разговорный эндпоинт для прямых вопросов агентам, и `/replay` — перезапуск прошлого инцидента через пайплайн без побочных эффектов.

---

## Какая инфраструктура нужна?

| Компонент | Роль |
|---|---|
| Kubernetes | Цель для действий; источник контекста деплоев/подов |
| AlertManager | Источник вебхуков (kube-prometheus-stack или standalone) |
| Redis | Очередь задач Celery + состояние аппрувов |
| PostgreSQL | Записи инцидентов, история разговоров, аудит |
| Discord бот | Нотификации и кнопки в канале аппрувов |
| Anthropic API key | Инференс LLM для всех агентов |

---

## Как настроить?

Скопируй `.env.example` в `.env` и заполни нужные значения:

```env
ANTHROPIC_API_KEY=sk-ant-…
DATABASE_URL=postgresql+asyncpg://user:pass@host/db
REDIS_URL=redis://:pass@host:6379/0
DISCORD_TOKEN=…
DISCORD_CHANNEL_ID=…        # ID канала для аппрувов
DISCORD_APPROVER_ROLE_ID=…  # Discord-роль с правом аппрувить
```

Затем:
```bash
docker compose up -d
```

API стартует на порту 8000. `/healthz` — liveness, `/readyz` — readiness (делает `SELECT 1` к PostgreSQL).

---

## Как работает интеграция с AlertManager?

Добавь webhook-ресивер в `alertmanager.yml`:

```yaml
receivers:
  - name: ai-copilot
    webhook_configs:
      - url: 'http://sre-copilot:8000/webhooks/alertmanager'
        send_resolved: true
```

Эндпоинт не имеет аутентификации по умолчанию — ограничь его на сетевом уровне (Kubernetes NetworkPolicy или IP-аллоулист в ingress), чтобы к нему мог достучаться только AlertManager.

Каждый алерт в пейлоаде генерирует отдельную задачу Celery и независимо проходит весь пайплайн агентов.

---

## Что происходит с каждым алертом?

```
Алерт приходит на POST /webhooks/alertmanager
  → создаётся IncidentRecord (статус: PENDING)
  → задача Celery: process_incident
  → агенты Analyzer → Hypothesis → Critic → Fix → Risk
  → IncidentRecord обновляется (статус: COMPLETED, анализ прикреплён)
  → репорт отправляется в Discord
  → если сгенерирован ExecutionIntent: запрос аппрува в Discord
```

---

## Что такое SAFE_MODE?

`SAFE_MODE=true` (по умолчанию): любой `ExecutionIntent` с деструктивным действием (рестарт, скейл, удаление) требует аппрув в Discord перед выполнением.

`SAFE_MODE=false`: действия выполняются сразу без аппрува. Используй только на dev/staging, где скорость важнее проверки, и только при ограниченном источнике вебхуков.

Никогда не ставь `SAFE_MODE=false` в проде.

---

## Как работает флоу аппрувов?

1. Пайплайн агентов генерирует `ExecutionIntent`
2. В Discord отправляется сообщение с описанием действия + кнопки Approve / Reject
3. Авторизованный пользователь (по роли или ID из конфига) нажимает Approve
4. Внутренне вызывается `POST /approvals/{id}/approve`
5. Действие выполняется через DSL `ExecutionIntent` → транслятор kubectl
6. Результат постится в Discord

Намерения протухают через 30 минут по умолчанию. Протухшие аппрувить нельзя.

---

## Кто может апрувить?

Задай хотя бы одно из:
```env
DISCORD_APPROVER_ROLE_ID=123456789
DISCORD_APPROVER_USER_IDS=111222333,444555666
```

Без этого аллоулиста любой участник канала может апрувить действия. Настрой до того как копилот начнёт получать боевые алерты.

---

## Как настроить тиры неймспейсов в k8s_guard.py?

Редактируй `app/services/k8s_guard.py`. Неймспейсы классифицируются по регулярке к имени:

```python
TIERS = {
    "production": re.compile(r"^prod.*|.*-prod$"),
    "staging":    re.compile(r"^stag.*|.*-staging$"),
    "dev":        re.compile(r"^dev.*|.*-dev$"),
}
```

- `production` — всегда требует аппрув, игнорирует SAFE_MODE
- `staging` — требует аппрув при SAFE_MODE=true
- `dev` — выполняется напрямую
- Неймспейсы, не попавшие ни под один паттерн — дефолтятся в `production`

Якори паттерны через `^` и `$`, чтобы не было матча по подстроке.

---

## Можно протестировать пайплайн без реального Kubernetes?

Установи `K8S_DRY_RUN=true` в `.env`. Пайплайн запускается полностью (AlertManager → агенты → аппрув в Discord), но действия логируются вместо выполнения. Удобно для валидации работы агентов и флоу аппрувов до выхода в прод.

---

## Какую модель использует и можно ли сменить?

По умолчанию: то, что задано в `ANTHROPIC_MODEL` (например `claude-opus-4-7`). Многошаговый пайплайн выигрывает от мощных моделей — меньшие могут генерировать структурно невалидные `ExecutionIntent`, которые DSL-валидатор тихо отклоняет.

---

## Как переиграть исторический инцидент?

```http
POST /replay
Content-Type: application/json

{"incident_id": "uuid-существующего-инцидента"}
```

Replay перезапускает пайплайн агентов на сохранённых данных без нотификаций в Discord и без действий в Kubernetes. Используй для тестирования изменений в промптах или проверки поведения агентов на прошлых инцидентах.

---

## Что такое /copilot?

`POST /copilot` — разговорный эндпоинт для прямых вопросов агентам анализа без AlertManager. Запускает до 3 итераций анализа, пока уверенность не достигнет порога 0.7. Требует JWT-аутентификацию — задай `JWT_SECRET` в `.env`.

---

## Куда сообщать об ошибках?

[GitHub Issues](https://github.com/froggychips/sre-ai-copilot/issues) или Telegram [@froggychips](https://t.me/froggychips).
