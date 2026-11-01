# sre-ai-copilot — FAQ

Практические ответы по развёртыванию и настройке AI-помощника SRE.

---

## Что это делает?

Боевой путь — детерминированный, без LLM. Alertmanager шлёт алерты в копилот → тот пишет их в граф знаний (KG) и для prod/preprod и алертов уровня нод отправляет в Discord обогащённую карточку (владелец, недавние деплои, blast radius, факты правил). Для нескольких классов алертов мост remediation (`app/remediation/`) подбирает playbook и постит карточку с кнопками «Применить / Отклонить»; оператор из allowlist одобряет → действие выполняется в Kubernetes отдельным процессом-исполнителем → результат проверяется и дописывается в ту же карточку.

Многоагентный LLM-пайплайн (Analyzer → Hypothesis → Critic → Fix → Risk, эндпоинт `POST /webhooks/alertmanager`) в коде есть, но **в проде выключен** (`LLM_PIPELINE_ENABLED=false`, ни один маршрут на него не ведёт). То же относится к `/copilot` (разговор с агентами) и `/replay` (перезапуск прошлого инцидента без побочных эффектов): они есть, но требуют LLM-бэкенда.

---

## Какая инфраструктура нужна?

| Компонент | Роль |
|---|---|
| Kubernetes | Источник контекста (поды, деплои, события); цель одобренных действий |
| Alertmanager | Источник вебхуков (в проде VMAlertmanager, `k8s/vmalertmanagerconfig.yaml`) |
| Redis | Брокер Celery (очереди `celery`, `sync`, `executor`) + дедуп и состояние аппрувов |
| PostgreSQL | Граф знаний, записи инцидентов, решения, аудит |
| Discord бот + вебхук | Карточки алертов, кнопки, дайджесты |
| Anthropic API key | Только для LLM-пайплайна (`LLM_BACKEND=anthropic`); пока он выключен — не нужен |

---

## Как настроить?

Скопируй `.env.example` в `.env` и заполни нужные значения:

```env
DATABASE_URL=postgresql+asyncpg://user:pass@host/db
REDIS_URL=redis://:pass@host:6379/0
DISCORD_WEBHOOK_URL=…           # embed-ы инцидентов
DISCORD_BOT_TOKEN=…             # bot API — нужен для кнопок Применить/Отклонить
DISCORD_INCIDENT_CHANNEL_ID=…   # канал для сообщений об инцидентах с кнопками
DISCORD_APPROVERS_USER_IDS=…    # CSV ID пользователей Discord с правом жать кнопки
ANTHROPIC_API_KEY=sk-ant-…      # только если включаешь LLM-пайплайн
```

Затем:
```bash
docker compose up -d
```

API стартует на порту 8000. `/healthz` — liveness, `/readyz` — readiness (делает `SELECT 1` к PostgreSQL). В проде копилот разворачивается из манифестов `k8s/` через `deploy.sh` (Helm-чарт удалён в 1.0.28).

---

## Как работает интеграция с AlertManager?

Принимающих эндпоинтов три; боевая маршрутизация — в `k8s/vmalertmanagerconfig.yaml`:

| Эндпоинт | Что делает | Кто туда маршрутизируется |
|---|---|---|
| `POST /webhooks/alertmanager/enrich-and-forward` | запись в KG + обогащённая карточка в Discord | `prod-*`, `preprod-*`, алерты без namespace (ноды, control plane) |
| `POST /webhooks/alertmanager/store` | только запись в KG, в Discord ничего (осознанно) | `squad-*`, `preupdate-*`, `severity=info` |
| `POST /webhooks/alertmanager` | полный LLM-пайплайн | никто (`LLM_PIPELINE_ENABLED=false`) |

Watchdog и `severity=none` уходят в blackhole.

Эндпоинты **fail-closed**: нужна подпись HMAC-SHA256 (`X-Alertmanager-Signature`, ключ `ALERTMANAGER_WEBHOOK_SECRET`), без настроенного секрета любой запрос получает 401. Штатный AlertManager подписывать тело не умеет, поэтому вариантов два (подробно — в [SECURITY.md](../SECURITY.md) и `k8s/vmalertmanagerconfig.yaml`):

- подписывающий прокси рядом с AlertManager (предпочтительно; поставь `ALERTMANAGER_REQUIRE_SIGNED_TIMESTAMP=true`);
- либо явный opt-out `ALERTMANAGER_ALLOW_UNAUTHENTICATED=true` **и** NetworkPolicy, пускающая на порт 8000 только namespace AlertManager. Opt-out без такой NetworkPolicy оставляет эндпоинт открытым для любого пода кластера.

---

## Что происходит с каждым алертом?

```
/enrich-and-forward (prod, preprod, ноды)
  → KG: kg_alerts / kg_incidents, атрибуция к k8s-объекту (app/services/alert_target.py)
  → дедуп (повтор горящего алерта карточку не постит)
  → детерминированное обогащение (факты правил, владелец, деплои, blast radius)
  → карточка в Discord, канал инцидентов
  → если может подойти playbook: задача remediation_bridge в Celery

/store (сквады, preupdate, info)
  → только KG; в Discord — лишь карточка предложения remediation, когда
    сматчился применимый playbook с policy «approve»

/webhooks/alertmanager (LLM-пайплайн, в проде выключен)
  → IncidentRecord → Celery process_incident → Analyzer → Hypothesis → Critic → Fix → Risk
  → при LLM_PIPELINE_ENABLED=false задача выходит без вызовов LLM
```

---

## Почему алёрт пришёл серым, с иконкой 🔇 и без пинга/@mention?

Это **намеренное подавление на этапе рендера** (alert-quality), не баг и
не дроп. Карточку специально оставляют **видимой** (чтобы её можно было
проверить глазами), но приглушают: серый цвет + 🔇, без 🚨 и без
@mention. Severity при этом **не** понижается — приглушается только
рендер.

Приглушают по одной из трёх причин:

- **`🔇 META-AGGREGATE`** (`meta_noise`, env `META_NOISE_ENABLED`):
  мета-агрегат — счётчик `*NewCriticalAlerts` либо производный
  control-plane scrape-gap алёрт (`etcdInsufficientMembers` /
  `ScrapePoolHasNoTargets` / `RecordingRulesNoData`). Каждый реальный
  критикал и так приходит отдельной громкой карточкой, поэтому агрегат не
  несёт сигнала.
- **`🔇 GENERATION-CHURN`** (`gen_mismatch_noise`, env
  `GEN_MISMATCH_NOISE_ENABLED`): `KubeDeploymentGenerationMismatch` при
  **здоровых** репликах (`ready==desired`). Внешний контроллер (Rancher)
  штатно бьёт `metadata.generation`; сам накат давно сошёлся. **Важно:**
  если реплики деградированы (`ready<desired` или неизвестны) — карточка
  остаётся **громкой** (fail-safe loud): реальный зависший накат всё
  равно звенит.
- **rollout-окно** (`rollout_noise`): алёрт пришёл в окне недавнего
  деплоя.

Отличие от полностью подавленного (dropped) алёрта: dropped
(`ALERT_SUPPRESS_NAMES`, Watchdog / InfoInhibitor) вообще не появляется в
Discord; приглушённый — появляется, но тихо.

Каждый класс можно вернуть в громкий режим, выставив его env-kill-switch
в `false`.

---

## Что такое SAFE_MODE?

`SAFE_MODE=true` (по умолчанию): любой `ExecutionIntent` с деструктивным действием (рестарт, скейл, удаление) требует аппрув в Discord перед выполнением.

`SAFE_MODE=false`: действия выполняются сразу без аппрува. Используй только на dev/staging, где скорость важнее проверки, и только при ограниченном источнике вебхуков.

Никогда не ставь `SAFE_MODE=false` в проде.

---

## Как работает флоу аппрувов?

В проде кнопки приходят из детерминированного моста remediation (с 1.0.31; исполняется в Celery с 1.0.32):

1. Алерт, чей `alertname` есть у исполнимого (v2) playbook-а, ставит в Celery задачу `remediation_bridge` (дедуп постановки — Redis `SET NX`).
2. Задача собирает факты правил + состояние пода (K8sFacts), `match_playbooks` проверяет `match` и `preconditions`, intent собирает сервер из `plan.steps` playbook-а (не модель), policy решает `approve` / `block`.
3. `approve`: снимок цели (uid), серверный dry-run, `IncidentRecord(APPROVAL_PENDING)` + `kg_remediation_decisions` и карточка с кнопками «Применить / Отклонить» (`approve:` / `decline:`) в `DISCORD_INCIDENT_CHANNEL_ID`. `block` (prod/preprod/system): кнопок нет, только строка «playbook X: заблокирован policy» в обычной карточке.
4. Оператор из `DISCORD_APPROVERS_USER_IDS` нажимает кнопку. Клик приходит на `POST /discord/interactions`; подпись Ed25519 проверяется по `DISCORD_PUBLIC_KEY`.
5. При `EXECUTOR_DISPATCH=queue` запись выполняет `copilot-executor` (очередь `executor`, отдельный ServiceAccount с правом записи) под `K8sSecurityGuard`.
6. После записи идут verify-проверки playbook-а; итог дописывается в ту же карточку.

Мосту нужны `REMEDIATION_BRIDGE_ENABLED`, `EXECUTOR_ENABLED` и `EXECUTOR_APPROVAL_ENABLED`; `REMEDIATION_BRIDGE_NAMESPACES` ограничивает его префиксами namespace (пусто — все). В LLM-пайплайне (в проде выключен) intent приходит от FixAgent и проходит тот же gate и ту же кнопку.

---

## Кто может апрувить?

Задай хотя бы одно из:
```env
DISCORD_APPROVERS_USER_IDS=111222333,444555666
DISCORD_APPROVERS_ROLE_IDS=123456789
```

Оба пустые = fail-closed: любой клик Применить/Отклонить отклоняется и пишется в аудит (`app/api/discord_interactions.py`). Боевое развёртывание использует только allowlist пользователей, роли не настроены. Настрой до того, как копилот начнёт получать реальные прод-алерты.

---

## Куда копилот может писать?

Списки namespace — в `app/security/namespaces.py`; их импортируют и `K8sSecurityGuard` (`app/services/k8s_guard.py`), и DSL исполнения:

- `FORBIDDEN_NAMESPACES` — не трогаются никогда, даже на чтение (`kube-system`, `kube-public`, `kube-node-lease`, `chaos-mesh`, `mcp`);
- `READ_ONLY_NAMESPACES` — точное имя или префикс `<env>-` для `prod`, `preprod`, `preupdate`, `production`: только чтение;
- `WRITE_NAMESPACE_PATTERNS` — единственное место, где разрешена запись (после аппрува): `^squad-\d+$`, `^squad-gd$`.

Policy remediation дополнительно классифицирует тир namespace (`app/remediation/risk_axes.py`): playbook-и разрешают `approve` только для `dev` / `squad` и дают `block` для `prod` / `preprod` / `system`.

---

## Можно протестировать пайплайн без реального Kubernetes?

Глобального dry-run для Kubernetes нет. Что есть:

- `EXECUTOR_ENABLED=true` при `EXECUTOR_APPROVAL_ENABLED=false` — intent проверяется `kubectl … --dry-run=server`, но кнопки Apply нет, записи не будет;
- `DISCORD_DRY_RUN=true` — сообщения в Discord логируются вместо отправки (локальный e2e).

---

## Какую модель использует и можно ли сменить?

По умолчанию: `MODEL_NAME` (`claude-sonnet-4-6`, если не переопределено в `.env`). Многошаговый пайплайн выигрывает от мощных моделей — меньшие могут генерировать структурно невалидные `ExecutionIntent`, которые DSL-валидатор тихо отклоняет.

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

`POST /copilot` — разговорный эндпоинт для прямых вопросов агентам анализа без AlertManager. Запускает до 3 итераций анализа, пока уверенность не достигнет порога 0.7. Требует JWT-аутентификацию (RS256) — задай `JWT_PUBLIC_KEY` в `.env`; в проде обязательны также `JWT_ISSUER` и `JWT_AUDIENCE`.

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

Playbook-и — YAML-файлы со строгой схемой в `app/remediation/registry/`,
загружаются `load_registry()` в `app/remediation/playbook.py` (`extra="forbid"`
— опечатка в любом ключе падает на parse, а не на использовании; файлы на `_`
пропускаются). Контракт: [REMEDIATION_PLAYBOOK_CONTRACT.md](REMEDIATION_PLAYBOOK_CONTRACT.md).
Версий схемы две:

- **`remediation.playbook/v1`** — только preview (слой Phase A из v0.13.0,
  PR #99): матчится, скорится по дискретным risk axes, policy выдаёт
  `auto` / `approve` / `block`, ничего не исполняется. Пример ниже.
- **`remediation.playbook/v2`** — исполнимый: `match.alertnames`,
  `preconditions` по фактам правил (`found` / `absent`; `unknown` не проходит
  никогда), `plan.steps` из действий, известных исполнителю, проверки
  `verify`. Мост remediation предлагает только v2. В реестре:
  `restart_crashloop_deployment` и `scale_out_cpu_throttled_deployment`.
  Мост берёт алерты обоих (`KubePodCrashLooping` / `PodCrashLooping`,
  `CPUThrottlingHigh`; approve только в dev/squad), но план собирает только
  из параметров, известных серверу: плану scale-out мост ставит `current_replicas` = живое `spec.replicas` и `replicas` = на одну больше (реплик нет или ноль — предложения нет); `resource_pressure` берётся из самого горящего `CPUThrottlingHigh`, а `crashloop: absent` — только из успешного снимка, где целевой под не нездоров (с 1.0.34). `cleanup_stale_failed_job` (v1, только
  `match.classification`) мост не берёт никогда.

`_executor_apply_gate.yaml` — не playbook, а серверная политика gate для
одобренных человеком apply; её грузит только `executor_gate`.

Чтобы добавить v1 — положи новый `*.yaml` (уникальный `name`) с секциями:

```yaml
schema_version: remediation.playbook/v1
name: cleanup_stale_failed_job          # уникальный; один playbook на файл
kind: remediation
description: |
  Что делает и когда безопасно.
match:                                   # когда playbook применим
  classification: stale_failed_job
  job_age_hours: {gte: 24}               # числовые условия: gte/lte/gt/lt/eq
  active_jobs: {eq: 0}
policy:                                  # вердикт по контексту
  auto:                                  # eligible для auto (когда приедет executor)
    namespace_tier: ["dev", "squad"]
    owner_kind: "CronJob"
  approve:                               # требуется human approval
    namespace_tier: ["dev", "squad"]
    owner_kind: ["None", "helm_hook"]
  block:                                 # никогда не действовать
    any:
      namespace_tier: ["prod", "preprod", "system"]
plan:                                    # шаблонная команда + read-only preview
  command: ["kubectl", "delete", "job", "{job_name}", "-n", "{namespace}"]
  preview: ["kubectl", "get", "job", "{job_name}", "-n", "{namespace}", "-o", "yaml"]
observe:                                 # сигналы success / failure
  timeout: 5m
  success: {job_exists: false}
  failure: {job_exists: true, new_job_failed: true}
```

Канонический пример — `app/remediation/registry/cleanup_stale_failed_job.yaml`;
см. [Roadmap → Execution](../README.md#roadmap--execution-1) про то, как
executor-трек (per-incident `kubectl --dry-run=server` + Discord Apply,
PR #23/#26/#27) соотносится с этим preview-слоем.

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

## Почему в графе два узла с одним именем?

С contract 2.4 у узла есть `node_kind`: k8s Service `auth` и Deployment
`auth` — **разные узлы** (`service` и `workload`), связанные ребром
`serves_traffic`.

До этого они были одной строкой (ключ `(namespace, name)`), из-за чего ребро
между ними физически не могло существовать — оно всегда получалось self-loop
и отбрасывалось. На живом графе так терялось 2092 ребра за тик синка, а в
графе оставалось 3.

Практическое следствие: искать узел по `(namespace, name)` теперь
недостаточно — запрос неоднозначен. Нужен `node_kind`, иначе
`.one_or_none()` бросит `MultipleResultsFound`. В коде это стережёт тест
`test_no_new_node_lookup_without_node_kind`.

## Почему orphan_pct не упал после появления serves_traffic?

Потому что `serves_traffic` намеренно не засчитывается как связность.

Это ребро Service → его собственный backing workload, то есть связь узла со
своей же реализацией. Когда оно появилось разом у всех сервисов с
selector'ом, orphan «улучшился» с 72.5% до 42.0% — при том что межсервисная
связность не изменилась вообще: все 1506 «вылеченных» получили ровно одно
ребро, на свой же workload.

Метрика не должна улучшаться от изменения схемы хранения, поэтому
`compute_orphan_stats` исключает `serves_traffic`. Подробности — `docs/METRICS.ru.md` §6.

## Почему beat отправил задачу, а она не выполнилась?

Скорее всего протухла по `expires` — и это штатное поведение.

У каждой периодической задачи есть окно жизни ≈90% интервала. Если воркеры
заняты и задача пролежала в очереди дольше, она отбрасывается: тик
`kg_external_probe`, отправленный полтора часа назад, отвечает на вопрос «как
дела сейчас» и бесполезен.

До введения `expires` очередь копилась молча — 230 задач, из них 94
`kg_external_probe`. Хуже, что за этим хвостом стоял `kg_topology_resources_sync`
и не выполнялся вовсе.

Проверить глубину очереди: `kubectl -n sre-ai exec redis-0 -- redis-cli LLEN celery`.
Тяжёлые синки KG (`kg_topology_sync`, `tc_deploys_to_kg`, `kg_storage_sync` и
другие, `SYNC_QUEUE_TASKS` в `app/workers/tasks.py`) идут в отдельную очередь
`sync`, её слушает `copilot-worker-sync` (с 1.0.28) — смотри и `LLEN sync`.
Если очередь стабильно растёт — воркеры не успевают, и это отдельная проблема,
которую `expires` только маскирует.

## Почему факт показан как `?`, а не `✓` или `✗`?

С v1.0.4 у факта три исхода. `✓` (`found`) — правило нашло. `✗` (`absent`) — правило искало в **живом** источнике и не нашло, это доказательство отсутствия. `?` (`unknown`) — правило **не смогло проверить**: источник упал, поток деплоев не пополняется, сервиса нет в графе, у алерта нет времени. Потребители трактуют `?` как «нет доказательств ни в одну сторону»: критик никогда не опровергает им гипотезу, а Discord пишет «❔ Deploy-связь не проверена» вместо молчания. См. `docs/SEMANTIC_CONTRACT.md` §5.1.

## Что такое инцидент в графе и почему мой помечен `noise`?

В `kg_incidents` не больше одного открытого инцидента на `(namespace, service)`; алерты сервиса присоединяются к нему, флаппинг переоткрывает его в окне 30 минут вместо нового, закрывается он, когда все алерты resolved. `noise=true` значит, что обогащение сочло **все** его алерты шумом — обычно `KubeDeploymentGenerationMismatch` при здоровых репликах (327 из ~390 недельных алертов на живом кластере), мета-агрегаты или rollout в процессе. `GET /kg/incidents` такие скрывает по умолчанию (`include_noise=true` показывает); настоящий алерт на том же сервисе снимает флаг.

## Что значат ✓ ◇ ≈ ⌛ ⚠ в поле «Blast radius»?

Это эпистемические метки из `app/knowledge_graph/epistemic.py`: ✓ observed (endpoints с готовыми подами, runtime), ◇ declared (прочитано из k8s-манифеста — Service, Ingress), ≈ inferred (env-переменная, имя секрета, соглашение об именовании), ⌛ stale (не подтверждалось 7+ дней), ⚠ contradicted (источники спорят — например топология говорит «обслуживает», endpoints — «ноль готовых подов»). Пострадавший сервис наследует слабейшее звено своего пути. Все рёбра `calls` в графе сегодня declared или inferred — runtime-наблюдений вызовов пока нет, поэтому поле говорит и «список вызывающих может быть неполным».

## Почему мой Apply отказан с `target_reincarnated`?

Перед записью исполнитель снимает живой объект и сверяет его `uid` с `target_ref`, записанным в графе при разборе. Другой `uid` значит, что Deployment удалили и создали заново под тем же именем после инцидента — разбор описывает объект, которого больше нет. Перезапустите разбор по текущему объекту, не переодобряйте. Если сверять было нечем, запись идёт, а `executor_applied.identity_check` говорит `unknown:*`. После записи Celery-задача перепроверяет исход через 5 и 15 минут (`executor_verification`).

## Куда теперь ложатся алерты `KubeJobFailed`?

На владельца Job, а не на `vm-kube-state-metrics`. Лейбл `service` у kube-алертов — экспортер метрики, цель — в `job_name`. С v1.0.8 `resolve_store_service` ищет Job в `kg_k8s_jobs` (владелец Job, затем его CronJob по шаблону имени `<cronjob>-<unix-минуты>`), откатывается к имени CronJob и только для ad-hoc Job оставляет имя Job. Исторические строки переносит `python -m app.scripts.reattribute_job_alerts --apply`.

## Куда сообщать об ошибках?

[GitHub Issues](https://github.com/froggychips/sre-ai-copilot/issues) или Telegram [@froggychips](https://t.me/froggychips).
