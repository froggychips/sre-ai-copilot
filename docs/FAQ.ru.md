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

## Как KG узнаёт владельца сервиса?

Multi-signal owner inference (`app/services/ownership_suggester.py`),
добавлен в Wave 8 (PR #85). Четыре сигнала пробуются параллельно:

1. **Prefix** (weight 0.4) — regex по ns-имени (`squad-N-*` → `@squad-N`,
   `monitoring` / `kube-system` → `@platform`).
2. **Deploy history** (weight 0.4) — most-frequent `triggered_by` за 30
   дней по `kg_deployments`. Username → team handle через
   `owner_aliases.py`.
3. **Labels** (weight 0.2) — k8s labels `team` / `owner` / `squad` /
   `app.kubernetes.io/part-of` из `kg_services.metadata_json`.
4. **Manual override** — `OWNERSHIP_MANIFEST_PATH=ownership.yaml`, match
   по glob → confidence=1.0, оверрайдит всё.

Победитель — top-1 по сумме `weight × signal_strength`. См.
[Ownership manifest в RUNBOOK](RUNBOOK.ru.md#ownership-manifest) — как
добавлять override-ы.

---

## Почему в daily digest пишет «(new baseline)»?

Daily stats digest показывает trend Δ24h для каждой метрики (alert
count, deploy success rate, fragile services count, …). Trend требует
yesterday-state-строку для сравнения.

Когда digest запускается впервые или yesterday-state была purg-нута —
сравнения нет, и мы пишем `(new baseline)` вместо фейкового `Δ +0`.
Wave 8-F (PR #90) добавил этот explicit placeholder; до него trend
молча показывал 0, что вводило в заблуждение.

Placeholder исчезнет на следующем digest-run-е (через 24 часа), когда
появится yesterday-state. Если видишь `(new baseline)` больше одного
раза подряд — state-persistence сломалась, проверить строки
`kg_stats_digest_state`.

---

## Что значит `stale_class` у сервиса?

Wave 8 (PR #86) добавил column `kg_services.stale_class` с тремя
значениями:

- **`active`** — deploy за последние 30 дней. Нормальное operating
  state.
- **`expected_stale`** — не катился 30d, но это норма: backup/cron/
  system паттерны (`*-backup`, `*-cron`, `kube-system`, `monitoring`)
  либо `infra`/`platform`-owned namespaces.
- **`suspicious_stale`** — нет deploys 30d, не подходит под expected-
  паттерны. Кандидат на retire/handoff investigation.

Column переписывается идемпотентно через `kg_sync.sync_namespace` на
каждом hourly sync. Stats digest скрывает `expected_stale` по умолчанию
через `STATS_HIDE_EXPECTED_STALE=true`, чтобы убрать шум. См.
[stale_class в RUNBOOK](RUNBOOK.ru.md#stale_class-на-kg_services) — как
реклассифицировать misclassified сервис.

---

## Почему `http_5xx_rate` / `p95_latency_ms` в kg_service_health всегда 0?

Потому что application `/metrics` сидят за JWT и отдают 401 scraper-у.
Фикс требует изменений в бэкенде — тикет WO-12483. Пока он не закрыт,
`health_score` — инфра-прокси (без app-level HTTP-сигнала).

Это **не** значит, что HTTP-сигнала нет вообще: ingress-level метрики
живые (по состоянию на 2026-06-10). Метрики nginx-ingress включены на
обоих контроллерах кластера WO (`--enable-metrics=true`, per-host
лейблы оставлены включёнными), скрейпятся через `VMPodScrape` в ns
`cattle-system` с `honorLabels: true`, а beat-задача
`kg_ingress_observations_sync` каждые ~10 минут пишет per-host/path
строки p95/p99/rps/4xx/5xx в `kg_ingress_observations` — 100% строк
слинкованы с `kg_services`. `error_5xx_rate = 0` при ненулевом `rps`
реально означает «ошибок нет», а не «нет данных».

См. [Поток ingress-метрик в RUNBOOK](RUNBOOK.ru.md#поток-ingress-метрик-kg_ingress_observations) —
что проверять, когда таблица перестала наполняться.

---

## Как добавить новый playbook (Phase A remediation)?

**Короткий ответ: Phase A в плане, не в коде.** На момент v0.12.0
copilot работает в **advisory-режиме по умолчанию**
(`EXECUTOR_ENABLED=false`) с opt-in стадией `executor`, которая делает
только `kubectl --dry-run=server` валидацию. Wave 8 — это «metadata +
UX polish» фундамент перед тем как строить Phase A (remediation pipeline).

Phase A добавит:

- Playbook registry — типизированный `RemediationPlaybook` с
  preconditions, actions и rollback steps.
- Confidence gating — только playbook-и с `confidence ≥ 0.8` и
  KG-quality сигналами выше порога будут пытаться запускаться.
- Human-in-the-loop — Approve/Decline кнопки на каждом playbook-
  предложении (HIGH risk auto-apply никогда, by design).

План лежит в user-memory `project_remediation_pipeline_plan.md` (пока
не в репо). Когда Phase A приедет, эта запись FAQ будет обновлена с
реальными инструкциями по авторству playbook-ов.

Пока что см. [Roadmap → Execution](../README.md#roadmap--execution-1) в
README — executor-трек построен (PR #23/#26/#27) и закрыт за
`EXECUTOR_APPROVAL_ENABLED=true` для ad-hoc human-approved действий на
отдельных инцидентах.

---

## Как снять snapshot KG quality?

```bash
python -m app.scripts.quality_report --markdown --output baseline.md
```

CLI `quality_report` (PR #87) read-only — безопасно гонять на production.
Считает 5 секций (services, edges, events, coverage, quality flags),
output — markdown или JSON.

Использовать до/после крупных remediation-волн, чтобы видеть
измеряемый change. Baseline на v0.12.0:
`docs/quality_report_baseline_2026_05_24.md`.

---

## Куда сообщать об ошибках?

[GitHub Issues](https://github.com/froggychips/sre-ai-copilot/issues) или Telegram [@froggychips](https://t.me/froggychips).
