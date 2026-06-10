# SRE AI Copilot — Architecture

## 1. System Overview

The application consists of: an HTTP API (FastAPI), background tasks (Celery), a data layer (PostgreSQL), external MCP servers (integrations), supporting infrastructure (Redis, Discord, Kubernetes), and a fact-anchored diagnostic engine that feeds a multi-hypothesis LLM pipeline.

## 2. Runtime Components

- **API app (`app.main`)**: routing, auth dependency, metrics middleware, health/readiness endpoints.
- **Webhook pipeline (`app.api.webhooks` + `app.workers.tasks`)**: receives AlertManager payloads, builds context, runs the agent pipeline.
- **Copilot pipeline (`app.main:/copilot` + `app.celery_worker`)**: background conversational analysis with a confidence-threshold loop.
- **DiagnosticsEngine (`app.diagnostics.engine`)**: rule-based evaluation producing a typed `FactStore`.
- **FactStore (`app.diagnostics.facts`)**: canonical ground-truth about the incident; supports conflict detection and prompt serialization.
- **MultiHypothesisAgent (`app.agents.multi_hypothesis`)**: fan-out to 4 perspectives (app/infra/deps/runtime) with precondition filtering.
- **FactCriticAgent (`app.agents.fact_critic`)**: adversarial grounding — eliminates hypotheses that contradict observed facts.
- **FixAgent (`app.agents.fix`)**: generates a structured `ExecutionIntent`; recurrence-aware and Jira-enriched.
- **SimilarIncidentEngine (`app.core.intelligence.similar_incidents`)**: KG-based recurrence detection (7-day window).
- **JiraClient (`app.context.jira_client`)**: Atlassian REST API enrichment for FixAgent context.
- **Knowledge Graph (`app.knowledge_graph.*`)**: auto-populating directed graph in Postgres. Core tables: `kg_services`, `kg_service_edges`, `kg_deployments`, `kg_alerts`, `kg_pod_events`. Storage subgraph (PR #84): `kg_storage_volumes` (PVC/PV) + `kg_volume_edges` (heterogeneous). Jobs (PR #82): `kg_k8s_jobs` (Job/CronJob с `owner_service_id`). 10+ sync sources (env-vars, NATS env, DSN-from-secret-key, k8s events, k8s ingresses-as-host, **k8s Services + Ingresses (Wave 7-X)**, **NATS subjects from monorepo source (Wave 7-Z)**, **runtime PodEvent co-occurrence (Wave 7-Y)**, **k8s Jobs/CronJobs (PR #82)**, **PVC/PV storage (PR #84)**). Schema/quality contract — `app/knowledge_graph/contract.py` (KG_SCHEMA_VERSION=2.2); confidence scoring with multi-source provenance.
- **Alert enrichment (`app.services.alert_enrichment`)**: deterministic KG-based enrichment for `/webhooks/alertmanager/enrich-and-forward` — runs without LLM, ~5 SQL queries, builds `EnrichedContext` (recent_deploys, upstream_alerts, outgoing_deps, pod_events, jira_issues, primary_hypothesis, why_this_matters).
- **Data layer (`app.database`, `app.repository`)**: SQLAlchemy models and CRUD operations.
- **Integration layer (`app.services.mcp_client`)**: MCP client for k8s, TeamCity, and other external tools.
- **Safety services**: approval manager, K8s guard, execution DSL.

### Beat tasks (Celery, periodic)

| Task | Schedule | Purpose |
|---|---|---|
| `kg_topology_sync` | hourly @ :00 | k8s deployments → `kg_services` + edges (calls/uses_nats/uses_db) |
| `tc_deploys_to_kg` | every 15 min | TC builds → `kg_deployments` (multi-project, SUCCESS+FAILURE) |
| `k8s_pod_events_sync` | every 10 min | k8s Warning events → `kg_pod_events` |
| `kg_ingress_sync` | hourly @ :37 | k8s Ingresses → external entrypoint edges |
| `kg_alerts_resolve_sync` | every 15 min | refresh `kg_alerts.resolved_at` from AM API |
| `kg_drift_cleanup` | hourly @ :17 | services from missing namespaces → `synthetic=true` |
| `daily_stats_digest` | daily | KG-summary digest to Discord #stats |
| `chronic_alerts_digest` | every 6h | chronic-suppressed alerts visibility digest |
| `kg_metrics_sync` | every 5 min | VictoriaMetrics PromQL → `kg_service_health` (cpu_pct, mem_pct, restarts_rate, http_5xx_rate, p95_latency_ms — the last two are still always 0: app `/metrics` is behind JWT, pending WO-12483) |
| `kg_cluster_health_sync` | every 5 min | k8s node-level snapshot → `kg_cluster_observations` |
| `kg_ingress_observations_sync` | every ~10 min | nginx-ingress PromQL → `kg_ingress_observations` (per-host/path p95/p99/rps/4xx/5xx; live since 2026-06-10 — ingress metrics enabled cluster-wide) |
| `kg_anomaly_detect` | every 5 min | rolling robust-z scan → `kg_anomaly_observations` |
| `kg_signal_aggregates` | every 10 min | 24h roll-up of anomalies/alerts/deploys/pod_events → `kg_signal_aggregates` |
| `kg_seq_logs_sync` | every 10 min | Seq REST API → `kg_log_observations` (per service × level) |
| `kg_deploy_correlator` | every 15 min | recent incidents × deploys → multi-factor confidence + verdict |
| `team_digest` | daily @ 09:00 UTC | per-team fragile services digest |
| `kg_self_health_check` | every 30 min | 6 canaries against KG data quality |
| `kg_stuck_alerts_check` | hourly @ :11 | alerts firing >24h без resolved_at → escalation digest |
| `kg_topology_resources_sync` | every 15 min | **Wave 7-X**: k8s Service+Ingress declarative → edges `serves_traffic` + `routes_to` |
| `kg_runtime_correlation_sync` | every 30 min | **Wave 7-Y**: pod_event co-occurrence (7d window) подтверждает existing edges |
| `kg_nats_subjects_sync` | every 6h @ :43 | **Wave 7-Z**: parse C# monorepo → subject nodes + `uses_nats` edges с direction (off by default) |
| `kg_jobs_sync` | every 15 min | **Wave 8-A (PR #82)**: k8s Job/CronJob → `kg_k8s_jobs` + `runs_as_job` linkage (через `owner_service_id` metadata column) |
| `kg_storage_sync` | every 30 min | **Wave 8-B (PR #84)**: PVC/PV + `uses_volume` / `bound_to` edges в `kg_volume_edges` (heterogeneous Service↔PVC↔PV graph) |

## 3. Data Flow (Webhook Incident Pipeline)

```
AlertManager webhook
  → POST /webhooks/alertmanager
  → for each alert: IncidentRecord(status=PENDING)
  → Celery task: process_incident

  Stage 1: Context Builder
    → k8s_pod_state (live pod metadata)
    → vm_metrics (VictoriaMetrics memory/CPU)
    → teamcity_context (MCP — recent deploys)

  Stage 2: DiagnosticsEngine
    → rules: OOMKilledRule, ProcessCrashRule, CrashLoopRule, …
    → produces: FactStore{oom_killed, process_crash, crashloop, …}
    → conflict detection: MUTUALLY_EXCLUSIVE_PAIRS → cap confidence

  Stage 3: MultiHypothesisAgent
    → PERSPECTIVE_PRECONDITIONS filter (runtime requires process_crash)
    → parallel LLM fan-out per active perspective
    → FactCriticAgent grounding → survivors

  Stage 4: Jira Enrichment (best-effort)
    → JiraClient.search_by_service(service, namespace)
    → build_jira_context() → {open, resolved, has_open}

  Stage 5: FixAgent
    → recurrence-aware (_RECURRENCE_PREFIX if is_recurrence)
    → Jira-enriched (_build_jira_prefix if jira_context)
    → generates ExecutionIntent JSON

  Stage 6: RiskAgent → Discord approval
  Stage 7: IncidentRecord(status=COMPLETED, analysis=…)
  Stage 8: KG update (_is_quality_cause filter)
```

## 3a. KG-only Enrichment Flow (without LLM)

A parallel webhook path provides deterministic alert enrichment without LLM. Used as the primary Discord output channel; the full LLM pipeline above is gated behind `LLM_PIPELINE_ENABLED=False` by default.

```
AlertManager webhook
  → POST /webhooks/alertmanager/enrich-and-forward
  → store incident → KG (populate_from_incident)
  → group alerts by (alertname, severity)
  → for each group:
     → alert_enrichment.enrich_alert(db, incident)
        → recent_deploys_for(...)         # with adaptive effective_at
        → nearby_alerts(...)              # upstream alert correlation
        → incidents_on(...)               # recurrence window
        → _downstream_count_by_kind(...)  # inbound callers
        → upstream_of(..., fresh_only_days=30)  # outgoing deps with confidence
        → recent_pod_events_for(...)      # k8s diagnostic signal
        → JiraClient.search_by_service_sync(...)  # ticket linkback
        → PodEventsRule + RecentDeployRule + UpstreamDegradedRule
        → primary_hypothesis() + why_this_matters()
     → decide_send() (chronic suppress / rollout-silent)
     → DiscordService.send_enriched_alert(contexts, env, resurfaced)
        → severity-aware embed
        → confidence badges (●●●/●●○/●○○) with provenance
        → clickable TC build links + deployer name
```

Latency budget: <500ms p95 synchronous in HTTP handler. No LLM tokens. Total cost: 0 per alert.

## 3b. Active Observability Layer (Wave 1–5)

Beyond the discrete event tables (deployments / alerts / pod_events), the KG also materialises a continuous time-series view of every service, used by the digest, the anomaly detector, the deploy correlator, and the Discord pipeline.

### Time-series materialization (Wave 1)

| Table | Source | Granularity |
|---|---|---|
| `kg_service_health` | VictoriaMetrics PromQL via `metrics_sync.py` | 5-min, per service |
| `kg_cluster_observations` | k8s node API via `cluster_health_sync.py` | 5-min, per node |
| `kg_ingress_observations` | nginx-ingress PromQL via `ingress_observations_sync.py` | ~10-min, per host/path |
| `kg_signal_aggregates` | 24-h roll-up via `signal_aggregates.py` | hourly refresh, per service |

Every PromQL fetch is wrapped in a Postgres `SAVEPOINT` per row insert; an `IntegrityError` on the unique key (e.g. duplicate `(service_id, ts)`) rolls back to the savepoint and continues. This makes the sync naturally idempotent on overlapping windows.

Wave 5 PromQL gotcha (documented for future authors): `mem_pct` must be computed as `avg(rate(container_memory_working_set_bytes) / on(pod) kube_pod_container_resource_limits)` — i.e. divide first, aggregate second. The aggregate-then-divide form ran for several days returning silent 0s because the many-to-one join collapsed before the division. The kg_self_health canary (see below) was added specifically to detect this class of silent failure.

Coverage update (2026-06-10): nginx-ingress metrics are now enabled on both WO controllers (`--enable-metrics=true` on the shared `ingress-nginx-controller`, 16 nodes, and `ingress-prod-controller`, 5 prod nodes). Per-host labels are kept — `metrics-per-host` defaults to true and must NOT be disabled: the copilot sync filters by host. Scraping goes through VMPodScrape `ingress-nginx-shared` + `ingress-prod` in ns `cattle-system` with `honorLabels: true` (mandatory — otherwise the `namespace` label gets overwritten into `exported_namespace`), picked up by the VMAgent in ns `monitoring` (`selectAllByDefault`). As a result `kg_ingress_observations` is populated with per-host/path p95/p99/rps/4xx/5xx; the first 50 minutes produced 156 rows across 23 hosts covering all contours (prod/squad/preprod/preupdate/infra), with 100% of rows linked to `kg_services`.

Remaining honest limitation: per-service `http_5xx_rate` and `p95_latency_ms` in `kg_service_health` are STILL always 0 — `/metrics` of the ASP.NET services (Kestrel) is closed by JWT middleware (401). Backend ticket WO-12483 tracks the fix (recommendation: a separate management port without auth); once it ships, a VMServiceScrape on the app namespaces will be added and these fields come alive. Until then `health_score` remains an infra proxy (alerts + pod_events + aggregates), and `log_error_rate` is a log-derived proxy, not HTTP 5xx. The pipeline degrades gracefully — anomaly detection on a flat-zero metric simply produces no observations.

### Anomaly detection (Wave 2 + Wave 6 update)

`app/knowledge_graph/anomaly_detection.py` scans the materialised time series and writes `kg_anomaly_observations`.

- **Robust-z statistic.** `robust_z = (current − median(baseline)) / (1.4826 × MAD(baseline))`. Uses median + MAD instead of mean + stddev, so a single spike in the baseline doesn't poison the threshold.
- **Seasonal baseline.** When ≥50 historical points are available, baseline is stratified by hour-of-day to absorb daily traffic patterns. `extras.method = robust_z_seasonal`. Below that threshold the detector falls back to a flat baseline (`robust_z_flat`).
- **Configurable thresholds** via `KG_ANOMALY_ROBUST_Z_WARN` (default `3.5`) and `KG_ANOMALY_ROBUST_Z_CRIT` (default `6.0`).
- **Volume guard.** No more than 3 observations per (service, metric) per hour, to avoid flooding when a metric is genuinely sustained-anomalous.
- **Baseline window:** rolling 7 days.

### Deploy ↔ Incident correlator (Wave 2 + Wave 6 update)

`app/rca/deploy_correlator.py` is invoked inline by `IncidentPipeline._enrich_deploy_correlation` for every incident that reaches enrichment.

- **Window:** `[incident − 2h, incident]`. Every deployment of the affected service inside the window is a candidate.
- **Confidence formula.** Multi-factor score, each component clamped to `[0, 1]`:
  - `N_spikes`: number of anomaly observations of the service after the deploy
  - `max_zscore`: peak robust-z observed in the window
  - `time_proximity`: how close the deploy is to the incident start
  - `deploy_status_factor`: FAILED deploys score higher than SUCCESS
  - `flat_baseline_penalty`: if baseline was all-zeros (i.e. metric was dead anyway), confidence is dampened
- **Verdict tiers:**
  - `likely` if confidence ≥ 0.7 — surfaced as a "Suspect deploy" block in the Discord embed
  - `suspect` if 0.4 ≤ conf < 0.7 — surfaced with weaker language
  - `weak` if 0.2 ≤ conf < 0.4 — kept in the analysis JSON, not in the embed
  - `unlikely` if conf < 0.2 — recorded for audit but not shown
- Available downstream via `analysis["deploy_correlator"]` for the Discord renderer and any future RCA consumers.

### Seq logs integration (Wave 2)

`app/knowledge_graph/seq_logs_sync.py` polls the Seq REST API every 10 minutes and writes `kg_log_observations`, one row per `(service, level)` per 10-min window.

- Match algorithm: Seq event `Application`/`Service` property → `kg_services.name`, falling back to namespace prefix.
- Used by the Discord pipeline to render a "log error rate" block when anomalies coincide.
- Known limitation: at the moment the pipeline writes everything as `Information` because the available realms expose only a single combined stream — per-realm Seq instances or access-log routing would be needed to populate `Error`/`Warning` independently. The schema and downstream consumers are level-aware; only the source data is degraded.

### Daily team digest (Wave 2)

`app/services/team_digest.py` runs daily at 09:00 UTC and produces a per-team Discord embed with:

- Fragile top-5 services by `health_score` (the lowest scores)
- Deploy success-rate over the last 24h
- Open alerts breakdown (firing / chronic-suppressed / resolved)
- Top alertnames over the last 24h
- SLO burn rate where available

### Discord pipeline overhaul (Wave 3)

The Discord renderer (`app/services/discord_service.py`) was reshaped from "send and forget" into a stateful publisher:

- **Dedup window**: 30 min. Repeat of the same logical alert PATCHes the existing message via `webhook?wait=true` instead of re-posting.
- **Severity routing**: `critical` / `warning` → channel post; `info` / `none` → silent (audit only). Configurable per env.
- **Per-team channel routing** via `DISCORD_TEAM_CHANNEL_MAP` (JSON: `{team_owner: webhook_url}`); fallback to the default webhook.
- **Suspect deploy block** rendered when `deploy_correlator.verdict ∈ {likely, suspect}`.
- **Log error rate block** rendered when `kg_log_observations` shows an anomaly in the same window.
- **Anomaly block** rendered from `kg_anomaly_observations`.
- **Recurrence block** — "×N in 24h · M in 7d" — driven by KG history.
- **Linked alerts aggregation** — alerts whose upstream/downstream services are also firing are folded into one embed.
- **Persistent approval** — `ActionApproval` ORM table with UNIQUE `(incident_id, intent_signature)`; Approve / Decline buttons hit the Discord interactions endpoint and record the decision.

## 3c. Security & Operational Hardening (Wave 6)

### PII redaction

`app/services/pii_redaction.py` provides a regex-based redactor with patterns for:

- email addresses
- IPv4 and IPv6 literals
- JWT tokens (3-segment base64)
- `Bearer …` authorisation headers
- UUIDs
- long hex strings (likely fingerprints / keys)
- `key=value` style secrets where key matches `password|token|secret|api_key`

Applied at two layers:

1. **Write-time** in `seq_logs_sync.py` — log lines are redacted before being persisted into `kg_log_observations.message_sample`, with a 500-character truncation. This keeps the database itself clean.
2. **Defense-in-depth** at embed-render time in `discord_service.py` — every string heading to a Discord webhook is run through the redactor a second time, on the assumption that any future data source might bypass write-time scrubbing.

The redactor is idempotent: running it twice produces the same output.

### Approve / Decline authorization

The Discord interactions endpoint (`app/api/discord_interactions.py`) now gates Approve / Decline button presses:

- `DISCORD_APPROVERS_USER_IDS` — comma-separated allowlist of Discord user IDs
- `DISCORD_APPROVERS_ROLE_IDS` — comma-separated allowlist of Discord role IDs (the interaction member must hold at least one)
- `DISCORD_APPROVAL_RATE_LIMIT_PER_HOUR` — per-user quota, default 5

Semantics:

- **Fail-closed** when both lists are empty — the button is denied and audit-logged as `DISCORD_APPROVAL_DENIED_NO_APPROVERS_CONFIGURED`.
- Unauthorized presses get an ephemeral deny reply and audit log `DISCORD_APPROVAL_DENIED_UNAUTHORIZED`.
- Rate-limit is in-memory. An authz failure does NOT consume quota — a denied user can't be used to exhaust the limit for a legitimate approver.

### KG self-health canary

`app/knowledge_graph/self_health.py` is a "monitoring of the monitoring" beat task, triggered after the Wave 5 mem_pct silent-failure to make sure that class of regression is detected automatically.

Runs every 30 minutes. Six checks, each returning `ok` / `warn` / `fail`:

1. **`materialization_zero_rate`** — % of rows in `kg_service_health` where a metric is 0 or NULL over 24h. Allowlist for known-zero metrics (`http_5xx_rate`, `p95_latency_ms` while app `/metrics` stays behind JWT — WO-12483).
2. **`sync_lag`** — `max(ts)` per beat task vs expected interval; >2× → warn, >5× → fail.
3. **`anomaly_signal_health`** — count of `kg_anomaly_observations` over 24h. 0 → warn (flat baseline or detector broken). >500 → warn (threshold too loose, overload).
4. **`alerts_resolve_freshness`** — count of `kg_alerts` with `fired_at < 7d ago` and `resolved_at IS NULL`. >20 → warn.
5. **`pod_events_link_rate`** — % of `kg_pod_events` over 24h with `service_id NOT NULL`. <80% warn, <50% fail (StatefulSet resolver regression).
6. **`edges_freshness`** — % of `kg_service_edges` with `last_seen_at < 24h` or `NULL`. >30% stale → warn (kg_topology_sync regression).

Output: audit-log line per run; on any `fail`/`warn`, a single Discord embed is posted to `DISCORD_WEBHOOK_SELF_HEALTH_URL` — kept separate from `#infra-error` to avoid drowning operational alerts. A 6-hour dedup window prevents the same canary fail from spamming the channel.

## 3d. Topology Expansion (Wave 7)

Wave 7 расширяет источники топологии KG с двух (env-var heuristic +
Ingress-host externalisation) до пяти, плюс runtime confirmation channel.
Цель — дать confidence-фреймворку (`kg_service_edges.extras.discovery_sources`)
независимые tier-1 источники, чтобы провенанс edges не зависел от единственного
heuristic-парсинга env-переменных.

### Wave 7-X: declarative Service + Ingress parser

`app/knowledge_graph/k8s_topology_resources_sync.py` каждые 15 минут читает
`kubectl get services/ingresses -A -o json` и строит:

- **`serves_traffic`** edge: Service → backing Deployment, по selector-match
  на pod template labels. Declarative замена runtime Endpoint resolution —
  не зависит от живых pods, поэтому работает и для свёрнутых деплоев.
- **`routes_to`** edge: Ingress (как ресурс) → backend Service. Параллельный
  slice к существующему `k8s_ingress_sync.py`, который строит
  `ingress:<host>` → backend как `calls` (host-уровень). Один Ingress
  ресурс часто имеет N hosts/paths — этот модуль покрывает Ingress-as-resource,
  старый — Ingress-as-host. Оба пишутся в `extras.discovery_sources`
  через merge в `populator.upsert_edge`.

RBAC: cluster-role требует `services` + `ingresses` на `get`/`list`/`watch`
(см. `k8s/base/rbac.yaml`).

Включён по умолчанию (нет feature flag) — declarative-источник идемпотентен
и не требует внешних зависимостей кроме `kubectl`.

### Wave 7-Y: PodEvent runtime correlation

`app/knowledge_graph/runtime_correlation.py` каждые 30 минут ищет пары
сервисов, у которых warning-события (BackOff/Unhealthy/OOMKilled/
FailedScheduling/CrashLoopBackOff/FailedMount/ImagePullBackOff) сваливаются
в одном окне `RUNTIME_CORRELATION_WINDOW_MINUTES` (default 15 мин) N+ раз
за `RUNTIME_CORRELATION_LOOKBACK_DAYS` (default 7 дней).

**Что делает.** Подтверждает уже существующие edges новым
`discovery_source = "kg_sync/runtime_corr"` (tier-1 precedence 0.95). Это
дешёвый OTEL-substitute: вместо распределённого трейсинга смотрим на
наблюдаемую кореляцию failure-сигналов.

**Что НЕ делает.** Новые edges из ничего не создаёт. Симметричный сигнал
co-occurrence не определяет направление зависимости — поэтому это
confirmation channel, а не discovery channel. Топология строится
declarative-источниками (env, Service-selector, Ingress, NATS-monorepo).

**Synthetic-исключение.** Synthetic-узлы (NATS-cluster, ingress:host)
исключаются: их pod_events идут через cluster-wide kubelet и дадут
false-positive каждому сервису в namespace. См. `_is_synthetic()`.

Feature flag: `RUNTIME_CORRELATION_ENABLED=true` (включён по умолчанию).

### Wave 7-Z: NATS subjects parser

`app/knowledge_graph/nats_subjects_sync.py` каждые 6 часов клонирует
(shallow + sparse-checkout) WO monorepo, regex-парсит C# исходники на
NATS-consumers (`NatsJetStreamConsumer<T>.Subject => NatsSubjectConst.<NAME>`)
и publish call-sites (`SendToJetStreamAsync(subject: ...)`).

Subject регистрируется как synthetic-Service в namespace `nats-subjects`,
`name = subject:<value>` (например `subject:march-export`). Это
переиспользует существующую схему `kg_services`/`kg_service_edges` без
новых таблиц или миграций.

Для каждого call-site пишется edge `uses_nats` с `extras.direction ∈ {pub, sub}`,
`weight = count(call-sites)`. Это дополняет существующие `uses_nats` к
synthetic NATS-cluster-узлам (env-var-derived) — теперь видно не только
что сервис подключён к кластеру, но и какие именно subjects он публикует
или читает.

Service-name резолвинг: путь `GR.WO.Map.Service/...` → `map-service`
(lowercase, dots→dash) — matches Deployment-имена в k8s.
`NatsSubjectConst.<NAME>` → литеральная строка через один проход по
`GR.Platform/DataBus/Nats/NatsConst.cs`.

Feature flag: `NATS_SUBJECTS_PARSER_ENABLED=false` (выключен по умолчанию —
требует ssh-доступ к gitlab-monorepo и каталог `WO_MONOREPO_PATH`).
Переменные окружения: `WO_MONOREPO_PATH`, `WO_MONOREPO_SSH_URL`,
`WO_MONOREPO_SPARSE_DIRS`.

### Edge kinds (full inventory after Wave 7)

| Edge kind | Producer | Direction semantics |
|---|---|---|
| `calls` | `kg_sync` (env-var URL), `k8s_ingress_sync` (host) | A makes HTTP call to B |
| `uses_nats` (cluster-level) | `kg_sync._extract_nats_clusters` | Service uses NATS-cluster (shared/kingdom) |
| `uses_nats` (subject-level) | `nats_subjects_sync` | Service publishes/subscribes to a subject; `extras.direction ∈ {pub, sub}` |
| `uses_db` | `kg_sync` (secretKeyRef heuristic) | Service uses DB (without reading secret values) |
| `serves_traffic` (Wave 7-X) | `k8s_topology_resources_sync` | Service → backing Deployment (by selector) |
| `routes_to` (Wave 7-X) | `k8s_topology_resources_sync` | Ingress (resource) → backend Service |
| `runs_as_job` (Wave 8-A) | `k8s_jobs_sync` | CronJob/Job → owner Service (через `K8sJob.owner_service_id`, metadata-only) |
| `uses_volume` (Wave 8-B) | `k8s_storage_sync` | Service → PVC (в `kg_volume_edges`) |
| `bound_to` (Wave 8-B) | `k8s_storage_sync` | PVC → PV (в `kg_volume_edges`) |

## 3e. KG Metadata + UX Polish (Wave 8)

Wave 8 — это пакет из 17 PR, который доводит KG-метаданные и Discord
UX до состояния «можно строить Phase A remediation поверх». Контракт
становится формальным, добавляются два больших coverage-блока
(Jobs/Storage), owner inference перестаёт быть prefix-only, появляется
quality_report для baseline-снимков перед remediation-волной.

### Wave 8-A: k8s Jobs/CronJobs coverage (PR #82)

`app/knowledge_graph/k8s_jobs_sync.py` каждые 15 минут читает
`kubectl get jobs,cronjobs -A -o json` и upsert'ит в новую таблицу
`kg_k8s_jobs` (отдельная от `kg_services` — Job/CronJob НЕ "service",
не имеет постоянного pod-а).

- **Закрывает blind spot**: до Wave 8 KG видел только Deployments/
  StatefulSets; alembic migration jobs, backup CronJob-ы (etcd snapshot,
  push-s3), ad-hoc cleanup — оставались невидимыми. Failed alembic при
  rollout = прод stuck на старой схеме, никто не услышит.
- **Поля Jobs**: `succeeded_count` / `failed_count` / `active_count` /
  `completion_time` / `last_pod_exit_code`.
- **Поля CronJobs**: `schedule` / `last_schedule_time` /
  `last_successful_time` / `suspended`.
- **Semantic edge `runs_as_job`**: CronJob → owner Service реализован
  **через `K8sJob.owner_service_id` metadata-column** (не отдельный
  edge-row в `kg_service_edges`). Match через label
  `app.kubernetes.io/part-of` или `app` совпадающий с `kg_services.name`
  в том же namespace. Если matched нет — owner просто NULL.

### Wave 8-B: PVC/PV storage coverage (PR #84)

`app/knowledge_graph/k8s_storage_sync.py` каждые 30 минут (storage
редко меняется — claim создаётся ~раз в неделю, capacity статична, но
phase-переходы Bound→Released важны в течение получаса).

- **Закрывает blind spot**: ClickHouse / Postgres «упал» = в 95%
  случаев диск кончился; до Wave 8 KG не имел никакого storage-слоя.
- **Новые таблицы**: `kg_storage_volumes` (PVC/PV; `kind ∈ {pvc, pv}`;
  PV cluster-scoped — `namespace=''`) + `kg_volume_edges` (heterogeneous
  edges с tagged `src_kind`/`dst_kind`, без FK constraint).
- **Атрибуты**: storage_class, access_modes, phase
  (Bound/Pending/Lost/Released/Available/Failed), capacity_bytes.
- **Edges в `kg_volume_edges`**:
  - `uses_volume` (Service → PVC) — scan всех pod'ов cluster-wide; для
    каждого `pod.spec.volumes[].persistentVolumeClaim.claimName` → edge
    от owning Service (через ownerReference Deployment/StatefulSet/RS).
  - `bound_to` (PVC → PV) через `pvc.spec.volumeName`.
- **disk_pct enrichment** опционален через `STORAGE_METRICS_ENABLED=false`
  (default OFF — kubelet_volume_stats_* scrape config может быть не
  настроен; без него запрос вернёт 0 для всех PVC и замаскирует реальные
  NULL).

### Wave 8-C: multi-signal owner inference (PR #85)

`app/services/ownership_suggester.py` + `app/services/owner_aliases.py`.
Старая prefix-only логика расширена до взвешенного fusion-а трёх
независимых сигналов + manual override:

- **A. Prefix** (weight 0.4) — regex по ns-имени (`squad-N-*`,
  `<env>-kingdom<N>`, bare `monitoring`/`kube-system` → `platform`).
- **B. Deploy history** (weight 0.4) — most-frequent `triggered_by`
  из `kg_deployments` за 30 дней. Username транслируется через
  `owner_aliases.resolve_username` (YAML override + pre-baked defaults).
- **C. Labels** (weight 0.2) — k8s labels `team` / `owner` / `squad` /
  `app.kubernetes.io/part-of` из `kg_services.metadata_json`.
- **Manual override** через `OWNERSHIP_MANIFEST_PATH=ownership.yaml`
  (список `[{ns_pattern, owner, reason}]`, match по glob → confidence=1.0,
  все эвристики игнорируются).

### Wave 8-D: stale_class column (PR #86)

`kg_services.stale_class` (миграция `20260524_0200_add_kg_services_stale_class`)
+ `app/knowledge_graph/stale_classifier.py`:

- `active` — был deploy за последние `ACTIVE_WINDOW_DAYS` (default 30d).
- `expected_stale` — давно не катился, но это норма: backup/cron/system
  (`*-backup`, `*-cron`, ns `kube-system`, `monitoring`) либо
  infra/platform owner вне ACTIVE_WINDOW_DAYS.
- `suspicious_stale` — нет deploys 30d, не expected_stale.

`kg_sync.sync_namespace` переписывает column идемпотентно на каждом
sync; `stats_digest.stale_deployments_section` читает column как
primary с fallback на legacy `_classify_stale` (старая инсталляция без
свежего sync).

### Wave 8-E: KG schema/quality contract (PRs #83 / #89)

`app/knowledge_graph/contract.py` — единственный источник истины:

- `KG_SCHEMA_VERSION = "2.2"` (после Wave 7 → 2.1, после PR #82/#84/#86 → 2.2).
- `EDGE_KINDS` — реестр всех edge kinds + spec (semantic / src_kinds /
  dst_kinds / source / status / table). Поле `table` различает где
  edge живёт: `kg_service_edges` / `kg_volume_edges` / `fk_only` (через
  FK) / `metadata_only` (через owner_service_id).
- `OWNER_SOURCES` / `OWNER_SOURCE_ALIASES` — canonical источники
  owner-а + маппинг коротких имён из `ownership_suggester`.
- `STALE_CLASS_VALUES` — enum значений `kg_services.stale_class`.
- `STARTUP_CONTRACT_CHECK(db)` — boot-time диагностика: сверяет
  реальные kinds в БД с реестром, логирует drift.
- **Gate #22** (`tests/test_contract_drift.py`) — auto-validation что
  код и контракт не разъезжаются (CI-блокирует).

Человекочитаемая версия с quality metrics, compatibility policy и
edge inventory — `docs/KG_SCHEMA_CONTRACT.md`.

### Wave 8-F: Discord UX polish (PRs #76, #77, #79, #81, #90)

- **#76**: blast radius (X) / NATS impact (Z) / pod trail (Y) рендерятся
  в enriched embed как отдельные поля — до этого Wave 7 наполнял KG,
  но embed его не показывал.
- **#77**: PATCH-dedup для `send_enriched_alert` — 30-минутное content-key
  окно: если тот же логический алерт приходит повторно, PATCH-им
  существующее сообщение через `webhook?wait=true`. Параллельно к
  существующему fingerprint-dedup.
- **#79**: human-time (`2 hours ago` вместо ISO), pod name, ready/desired
  replicas, kubelet reason. Новый модуль `app/utils/time_human.py`.
- **#81**: stats digest UX — series→series collapse (один сервис с
  burst-pattern не разрывает digest), unowned action block (suggest owner
  через ownership_suggester), `trend Δ24h` для всех метрик, отдельные
  секции `chronic`/`resurfaced`, `stale classification` pill, переименование
  `fragile → blast-radius`.
- **#90**: cascade deploys aggregation (один push в `prod-shared` cascades
  на N kingdom realms → считаем как один деплой), new-baseline placeholder
  `(new baseline)` для Δ24h когда yesterday-state не существует.

### Wave 8-G: quality_report + snapshot fixtures (PRs #87, #88)

- **`app/scripts/quality_report.py`** — идемпотентный read-only CLI:
  считает 5 групп метрик из Postgres KG (services / edges / events /
  deploys / coverage). Markdown (default) или JSON output. Use case —
  точка отсчёта для Phase A (remediation), чтобы demonstrably улучшать
  metrics, а не угадывать. Baseline-снимок 2026-05-24:
  `docs/quality_report_baseline_2026_05_24.md`.
- **`tests/fixtures/discord_snapshots/`** — 7 known-good embed cases
  (critical_fresh / critical_resurfaced / warning_compact /
  burst_aggregation / daily_digest / chronic_digest / team_digest) с
  input.json + expected.json. **UX regression-guard**: любое изменение
  в discord/embed_builder валит diff. Update workflow:
  `UPDATE_SNAPSHOTS=1 pytest tests/test_discord_alert_gallery.py`.

## 4. Fact-Anchored Reasoning

The diagnostic engine runs before any LLM agent. It evaluates deterministic rules against structured k8s data (`k8s_pod_state`, pod events) and emits a `FactStore` — a typed collection of `Fact` objects, each with:

- `kind`: canonical slug (`FactKind.OOM_KILLED`, `FactKind.PROCESS_CRASH`, …)
- `observed`: whether the fact was confirmed
- `confidence`: 0.0–1.0 (capped on conflict)
- `evidence`: dict of supporting data

`FactStore.conflicts()` detects `MUTUALLY_EXCLUSIVE_PAIRS` (e.g. `{oom_killed, process_crash}`) and `_apply_conflict_signals()` caps confidence to 0.60, adds `conflict_with` to evidence, and appends a `<conflicts>` block to the prompt context visible to all agents.

LLM agents receive the FactStore serialized as a `<facts>` XML block. The FactCriticAgent uses it to adversarially ground each hypothesis — a hypothesis that contradicts confirmed facts is rejected.

## 5. Data Flow (Copilot Conversation)

```
POST /copilot
  → conversation persistence
  → Celery task: generate_reply
  → state: INVESTIGATING
  → context builder
  → up to 3 analysis iterations (confidence threshold 0.7)
  → states: HYPOTHESIS_GENERATED → FIX_PROPOSED
  → optional Discord notification
```

## 6. Recurrence Detection

`SimilarIncidentEngine` queries the KG for past incidents for the same service where:
- `resolution_quality = "resolved"` (quality-gate: `_is_quality_cause` passed)
- `resolved_at >= now() - RECURRENCE_WINDOW_DAYS` (default: 7 days)

If a match is found, `recurrence=True` is set in the pipeline output. `FixAgent` switches to investigative mode (`_RECURRENCE_PREFIX`) — it will NOT recommend a simple restart because that has already been tried.

## 7. Reliability & Observability

- Celery retries are configured on critical tasks.
- `/readyz` checks PostgreSQL availability with `SELECT 1`.
- Prometheus latency metrics are collected by the request middleware.
- OpenTelemetry tracing is initialized at startup (exports to Tempo if available).
- Structured audit log via `structlog` (stdout for production, file for local dev).

## 8. Security

- JWT-based user dependency for `/copilot`.
- `ALERTMANAGER_WEBHOOK_SECRET` HMAC validation for webhook endpoints.
- Guardrails at the DSL level (`ExecutionIntent`) and k8s policy validator (`K8sSecurityGuard`).
- Approval API for human-in-the-loop before any write action.
- `SAFE_MODE=true` is enforced in production (config validator raises on `SAFE_MODE=false` + `ENV=production`).
- Prompt injection guard (`prompt_guard.detect_injection`) with input length cap (`PROMPT_INPUT_MAX_CHARS`).
- **PII redaction** (`app/services/pii_redaction.py`) — write-time scrubbing of Seq log samples and defense-in-depth scrubbing of Discord embed strings. Patterns: emails, IPv4/IPv6, JWT, bearer tokens, UUIDs, long hex, `password|token|secret|api_key` key/value pairs.
- **Approve / Decline authz** on Discord buttons — `DISCORD_APPROVERS_USER_IDS` / `DISCORD_APPROVERS_ROLE_IDS` allowlists, `DISCORD_APPROVAL_RATE_LIMIT_PER_HOUR` quota. Fail-closed when both allowlists are empty.
- **Dedicated read-only Postgres role** (`kg_reader`) for any external KG access — separate user, `SELECT` on `kg_*` and `alembic_version` only, no default privileges (future tables must be granted explicitly).

## 9. External Integrations

| Integration | Purpose | Config keys |
|---|---|---|
| Kubernetes (MCP) | Pod state, logs, events, deployment control | `TEAMCITY_MCP_URL` (via wo-tools) |
| VictoriaMetrics | Memory/CPU metrics window before incident | `VICTORIA_METRICS_URL` |
| TeamCity (MCP) | Recent deploy context | `TEAMCITY_MCP_URL`, `TEAMCITY_MCP_TOKEN` |
| Atlassian Jira | Known open/resolved tickets for the service | `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` |
| Discord | Approval flow + incident report | `DISCORD_WEBHOOK_URL` |
| Discord (per-team) | Per-team channel routing for digests/incidents | `DISCORD_TEAM_CHANNEL_MAP` (JSON) |
| Discord (self-health) | KG canary alerts, separate from `#infra-error` | `DISCORD_WEBHOOK_SELF_HEALTH_URL` |
| Discord (authz) | Allowlist for Approve/Decline buttons | `DISCORD_APPROVERS_USER_IDS`, `DISCORD_APPROVERS_ROLE_IDS`, `DISCORD_APPROVAL_RATE_LIMIT_PER_HOUR` |
| Seq | Application log stream → `kg_log_observations` | `SEQ_URL`, `SEQ_API_KEY` |
| OpenTelemetry | Distributed tracing | `OTLP_EXPORTER_ENDPOINT` |
| Anomaly tuning | Robust-z thresholds | `KG_ANOMALY_ROBUST_Z_WARN`, `KG_ANOMALY_ROBUST_Z_CRIT` |
