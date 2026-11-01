# SRE AI Copilot — Metrics Pipeline

Обновлено: **2026-11-01** (v1.0.33). §1–§5 — конвейер метрик KG, §6 — метрики качества графа, §7 — собственные Prometheus-метрики копилота и алерты.

Карта потока метрик: какие сигналы есть в VictoriaMetrics, какие из них материализуются в Knowledge Graph, каким sync'ом, и — не менее важно — каких сигналов **нет** и что на самом деле означают их нули.

## 1. Карта сигналов (TL;DR)

| Сигнал | Таблица KG | Источник | Статус |
|---|---|---|---|
| `cpu_pct` / `mem_pct` / `restarts_rate` (per service) | `kg_service_health` | `kube_*` / cAdvisor через VMNodeScrape | ✅ live |
| `http_5xx_rate` / `p95_latency_ms` (per service) | `kg_service_health` | ASP.NET app `/metrics` | ❌ всегда 0 — блокировано WO-12483 |
| `p95` / `p99` / `rps` / `error_4xx_rate` / `error_5xx_rate` (per ingress endpoint) | `kg_ingress_observations` | `nginx_ingress_controller_*` через VMPodScrape | ✅ live с 2026-06-10 |
| Аномалии (robust-z) | `kg_anomaly_observations` | derived: `kg_service_health` + `kg_log_observations` | ✅ live |
| `orleans_latency_avg_ms` / `orleans_timedout_rate` / `orleans_messaging_fault_rate` / `orleans_pings_missed_rate` / `orleans_activation_churn` / `orleans_rerouted_rate` (per grainhost-сервис) | `kg_service_health` | `microsoft_orleans_*` через `VMPodScrape town-grainhost` (в чарте, порт 8080) | ✅ live там, куда доехал чарт (28 ns на 07.09.2026; prod ждёт) — здоровье силоса, **не HTTP RED**; латентность средняя (гистограммы нет) |
| `log_error_rate` | `kg_log_observations` | Seq REST API | ✅ live (лог-прокси, не HTTP) |
| `health_score` | `kg_services.health_score` | composite из KG-сигналов | ✅ live (инфра-прокси, не user-facing) |

## 2. Источники в VictoriaMetrics

Стек: vm-operator в ns `monitoring`, VMAgent с `selectAllByDefault` — любой объект `VMServiceScrape` / `VMPodScrape` / `VMNodeScrape` в любом namespace подхватывается автоматически.

- **`kube_*` / cAdvisor / kubelet** — VMNodeScrape-объекты из kube-stack чарта. Покрывают **весь кластер**: per-pod CPU, память, рестарты. Это основа `kg_service_health`.
- **PostgreSQL-экспортеры (bitnami) + NATS-экспортеры** — `VMServiceScrape`-объекты в app-namespace'ах, авто-создаются чартами (~518 объектов по кластеру). VMAgent подхватывает без ручной настройки.
- **nginx-ingress контроллеры** — `VMPodScrape`-объекты `ingress-nginx-shared` и `ingress-prod` (ns `cattle-system`, `honorLabels: true`). Метрики включены **2026-06-10** флагом `--enable-metrics=true` на обоих DaemonSet'ах. **`metrics-per-host` ОБЯЗАН оставаться включённым** — лейбл `host` — это то, по чему `ingress_observations_sync` строит PromQL; выключение молча убьёт `kg_ingress_observations`.
- **ASP.NET app-метрики — ОТСУТСТВУЮТ.** App-эндпоинт `/metrics` (Kestrel) закрыт JWT-middleware и отдаёт скрейперу 401. Бэкенд-тикет: **WO-12483**. После его раскатки останется завести `VMServiceScrape` на каждый app-namespace (метрике нужен лейбл `service`, чтобы `metrics_sync` её сматчил). До этого `http_requests_total` / `http_request_duration_seconds_bucket` в VM для app-namespace'ов не существуют.

## 3. Что пишется в KG и каким sync'ом

### `kg_service_health` ← `metrics_sync.py` (beat: `kg_metrics_sync`, ~10 мин)

Per-service `cpu_pct` / `mem_pct` / `restarts_rate` из `kube_*` / cAdvisor. Namespace-агрегированный PromQL (5 запросов на namespace вместо 5 на сервис — ~385 запросов суммарно против старых ~12300); резолв pod → service по longest-prefix-матчу против известных имён сервисов. Агрегация по нескольким pod одного сервиса: cpu/mem — mean, restarts — sum. Полностью нулевые ряды не вставляются (экспортёр не покрывает сервис). Идемпотентность по `UNIQUE(service_id, ts)`.

Колонки `http_5xx_rate` и `p95_latency_ms` существуют и PromQL валиден, но они **всегда 0 до WO-12483** — см. §2.

### `kg_service_health.orleans_*` ← `metrics_sync.py` (тот же beat, v1.0.9)

Discovery раз в тик — `count by (namespace) (microsoft_orleans_orleans_app_requests_latency_count{job=~".+/.+"})` — затем семь дополнительных PromQL только по namespace'ам с метером Orleans (28 из 231 на 07.09.2026), `by (pod)` и маппинг на сервисы как у cpu/mem. **Только pull-серии** (`job=~".+/.+"`, с 1.0.10): тот же метер силосы ещё и пушат в push-gateway (ns `monitoring`), где серии всех королевств схлопываются в поды `push-gateway-*` с `job` = имя пушера — без фильтра discovery находил `monitoring`, и сумма всех силосов (включая прод) ложилась в граф как здоровье сервиса `push-gateway` (латентность 52 с, 36 млн сбоев/мин). VM-operator ставит у pull `job = <ns>/<scrape>`, отбор позитивный: сменится формат — Orleans пропадёт целиком (`orleans_namespaces=0` в stats), а не превратится в мусор. Латентность взвешена по числу вызовов между подами (`Σ rate(sum) / Σ rate(count)`, мс); четыре rate — в минуту: `app_requests_timedout`, сбои доставки сообщений (`rejected|expired|sent_failed|sent_dropped`; `messaging_rerouted` исключён с 1.0.10 — пересылка на другой силос это стоимость, а не отказ, и при 10–14 тыс./мин в preprod она прятала 11,9 тыс./мин настоящих сбоев в squad-14), `messaging_pings_reply_missed` (прокси death-vote), churn активаций (`catalog_activation_created|destroyed|shutdown`), а с 1.0.11 — `orleans_rerouted_rate` (`messaging_rerouted`: пересылка на другой силос, намеренно отдельно от сбоев — рераны растут раньше сбоев при смерти силоса и при перекатке). **Семантика нуля:** prometheus-net не отдаёт счётчик до первого инкремента, поэтому отсутствующая серия сбоев у сервиса, у которого *есть* `latency_count`, пишется как `0.0` (сбоев не было), а `NULL` значит «силоса нет вовсе». Детектор аномалий сканирует эти колонки как остальные (`MIN_ABS_SPREAD_BY_METRIC`: 50 мс, 0.5, 1.0, 0.5, 50/мин, 100/мин); critical-embed получает поле «🧬 Orleans silo» с последним значением, суточной базой и ⚠ при росте больше +50 %. Потребители: `queries.orleans_health_for`.

### `kg_ingress_observations` ← `ingress_observations_sync.py` (beat: `kg_ingress_observations_sync`, ~10 мин)

Per `(ingress_name, host, path)`: `p95_latency_ms` / `p99_latency_ms` / `rps` / `error_4xx_rate` / `error_5xx_rate` из `nginx_ingress_controller_*`. Список host/path берётся из `kubectl get ingresses -A` (реюз парсера `k8s_ingress_sync`); backend-сервис резолвится в `service_id` через ingress backend → `kg_services` (namespace + name); если не зарезолвился — ряд всё равно пишется с `service_id = NULL`. Ряды, где все метрики 0, пропускаются (экспортёр не накрывает endpoint). Идемпотентность по `UNIQUE(ingress_name, host, path, ts)`.

Это **endpoint-уровневый** разрез (per host/path) — другой срез, чем per-service в `kg_service_health`. Сейчас это единственный источник реальных HTTP 5xx / latency в KG.

### `kg_anomaly_observations` ← `anomaly_detection.py` (beat: `kg_anomaly_detection_task`, ~10 мин)

Robust-z (median + MAD, сезонный baseline) по материализованным рядам: `cpu_pct` / `mem_pct` / `restarts_rate` из `kg_service_health`, плюс `log_error_rate` из Seq-логов (`kg_log_observations`). Anomaly detection на плоско-нулевой метрике (например `http_5xx_rate`) просто не порождает observations — graceful degradation, без false positive.

## 4. Семантика нулей и прокси (критично для потребителей)

Один и тот же ноль в разных таблицах означает противоположные вещи. Любой потребитель (digest, RCA, LLM-pipeline, люди) обязан прочитать эту секцию:

- **`kg_ingress_observations.error_5xx_rate = 0` при `rps > 0`** → реально нет ошибок. Трафик идёт, метрика живая, 5xx действительно ноль.
- **`kg_service_health.http_5xx_rate = 0`** → **НЕТ ДАННЫХ**, а не «нет ошибок». App-метрики не скрейпятся (WO-12483). То же для `p95_latency_ms`.
- **`health_score`** — **инфра-прокси**: деривируется из alerts, pod_events, deploy/SLO-агрегатов, а не из user-facing latency/errors. Высокий score = «cpu/события в норме», а **не** «нет 5xx».
- **`log_error_rate`** — **лог-прокси** (Error-строки из Seq), а не HTTP 5xx. Полезен как сигнал app-ошибок, но не взаимозаменяем с request error rate.

## 5. Как проверить поток (операционная шпаргалка)

Какие namespace'ы отдают ingress-метрики в VM:

```promql
# против http://vmsingle-vm-victoria-metrics-k8s-stack.monitoring.svc:8428
count(nginx_ingress_controller_requests) by (namespace)
```

Доезжают ли observations до KG и насколько они свежие:

```sql
SELECT count(*), max(ts) FROM kg_ingress_observations;
```

На месте ли scrape-объекты:

```bash
kubectl -n cattle-system get vmpodscrape
# ожидаем: ingress-nginx-shared, ingress-prod
kubectl -n cattle-system get ds -o yaml | grep -- --enable-metrics
# ожидаем: --enable-metrics=true на обоих controller DaemonSet'ах
```

---

## 6. Метрики качества графа — что они НЕ считают

Обновлено: **2026-08-08**.

Три метрики (`orphan_pct`, `owner_pct`, `app_scope`) считаются в
`contract.compute_orphan_stats` / `STARTUP_CONTRACT_CHECK` и попадают в
дайджест. У всех трёх есть неочевидные исключения — без них цифры меняются
от правок схемы, а не от состояния инфраструктуры.

### Считается только `node_kind='service'`

С contract 2.4 в `kg_services` живут узлы трёх типов. В метрики качества
попадают **только логические сервисы**. Workload-узлы (backing
Deployment/StatefulSet/DaemonSet) исключены: их 2871 на 8669 сервисов, и
включение удвоило бы знаменатель. Практически это выглядело бы как обвал
owner-coverage с 99.5% до ~50% в день выката — регрессия, которой нет.

### `orphan` не засчитывает `serves_traffic`

Это ребро Service → его собственный backing workload, то есть связь узла со
своей же реализацией, а не с другим сервисом. Пока типа узла не было, оно
вырождалось в self-loop и отбрасывалось; с `node_kind` стало настоящим и
появилось разом у всех сервисов с selector'ом.

Замер на проде 08.08.2026 сразу после выката:

| считаем | orphan |
|---|---|
| любое ребро | 2072 / 4933 → 42.0% |
| без `serves_traffic` (как сейчас) | 3578 / 4933 → **72.5%** |

Вторая строка совпадает со значением ДО выката: межсервисная связность не
изменилась вообще. Первая версия метрики отрапортовала бы улучшение вдвое,
которого не было.

**Правило:** метрика качества не должна улучшаться от изменения схемы
хранения. При добавлении нового типа узла или ребра надо перебрать все
метрики, которые считают «хоть что-нибудь», а не только те, где ждёшь
эффекта. Закреплено тестами `test_serves_traffic_alone_does_not_clear_orphan`
и `test_compute_orphan_stats_ignores_workload_nodes`.

### Что значит текущее значение

`orphan_pct ≈ 72%` — это НЕ «граф сломан». Знаменатель — real-сервисы без
`expected_stale`-инфраструктуры по всем контурам, включая dev/preprod, где
топология заведомо неполна. Отсутствие ребра означает «связь неизвестна», а
не «связи нет» (см. §7 в `KG_SCHEMA_CONTRACT.md`).

## 7. Собственные метрики копилота (перечень, v1.0.33)

Обновлено: **2026-11-01**. Каждое имя ниже сверено с `app/` (`Counter`/`Histogram` из `prometheus_client` и собственные `Collector`-ы).

### Где экспортируются

| Процесс | Endpoint | Скрейпит `VMPodScrape sre-ai-copilot` |
|---|---|---|
| `sre-ai-api` | `:8001/metrics` (`start_http_server` в `app/main.py`) плюс два Collector-а ниже | да |
| `copilot-worker`, `copilot-worker-sync` | `:8001/metrics`, multiprocess-режим (`app/observability/worker_metrics.py`, `PROMETHEUS_MULTIPROC_DIR`) | да |
| `copilot-executor` | тот же сервер воркера (`celeryd_init`), порт `metrics` :8001 объявлен с 1.0.34 | да — `pipeline_executor_applied_total`, `remediation_attempt_transitions_total` из `apply_intent` (до 1.0.34 не собирались) |
| `copilot-beat` | сервера нет | перечислен в селекторе, остаётся пустым таргетом |

NetworkPolicy пускает на :8001 только из ns `monitoring`.

### Collector-ы (считаются в момент скрейпа, только в API)

| Метрика | Тип | Смысл |
|---|---|---|
| `copilot_self_health_status` | gauge | Общий вердикт последнего прогона self-health: 0 ok, 1 warn, 2 fail, 3 unknown |
| `copilot_self_health_check_status{check}` | gauge | Вердикт по проверке (15 проверок: `sync_lag`, `source_coverage`, `graph_integrity`, `schema_version`, …); источники `disabled` в худший-из не входят |
| `copilot_self_health_last_run_timestamp` | gauge | Unix-время снимка в Redis (`stats:self_health:last`) |
| `celery_queue_length{queue}` | gauge | Задачи, ждущие воркера, по очередям брокера (`LLEN`): `celery`, `sync`, `executor`. Redis недоступен → серий нет (не ноль). С 1.0.28; до этого — Gauge, который никто не выставлял |

Нет снимка / нет Redis → серий нет, а не ноль.

### Процесс API

| Метрика | Тип | Смысл |
|---|---|---|
| `request_duration_seconds` | histogram | Латенси HTTP-запросов (middleware) |
| `alerts_suppressed_total{reason, alertname}` | counter | Алерты, отброшенные до карточки: `allowlist`, `self_noise`, `inhibited_warn`, `repeat` (повтор горящего алерта от Alertmanager, 1.0.31), `controller_lag_rancher_churn` (churn GenerationMismatch на `/store`) |
| `alertmanager_alerts_truncated_total{endpoint, receiver}` | counter | Алерты, обрезанные Alertmanager-ом по `max_alerts` (`truncatedAlerts` в payload) — часть группы до копилота не дошла |
| `webhook_replay_rejected_total{source}` | counter | Подписанные запросы вебхука, отклонённые как повтор (`local` — кэш процесса, `shared` — Redis) |
| `webhook_replay_shared_store_errors_total` | counter | Сбои Redis-хранилища nonce — anti-replay деградировал до процесса |
| `rate_limit_redis_errors_total{limiter}` | counter | Проверки rate-limit, не достучавшиеся до Redis |
| `rate_limit_local_fallback_total{limiter, decision}` | counter | Решения запасного лимитера в процессе |

### Процессы воркеров — диагностика, remediation, LLM-слой

| Метрика | Тип | Смысл |
|---|---|---|
| `diagnostic_facts_verdict_total{kind, verdict}` | counter | Факты по вердикту свидетельства — доля `unknown` по kind напрямую меряет Known Unknowns |
| `diagnostic_facts_observed_total{kind, observed}` | counter | Старый двузначный счётчик, оставлен ради дашбордов |
| `remediation_verification_total{outcome}` | counter | Исходы `remediation_verify`: `verified` / `failed` / `pending` / `unknown` |
| `remediation_attempt_transitions_total{from_status, to_status}` | counter | Переходы статуса в `kg_remediation_attempts` |
| `pipeline_executor_applied_total{success}` | counter | Реальные исходы apply после аппрува |
| `pipeline_stage_duration_seconds{stage}` | histogram | Латенси по стадиям |
| `pipeline_executor_status_total{status}`, `pipeline_execution_intent_total{parsed, action}`, `pipeline_resolution_quality_total{quality}`, `pipeline_incident_recurrence_total{is_recurrence}`, `pipeline_incident_flapping_total`, `pipeline_fact_conflicts_total{kind_a, kind_b}`, `pipeline_no_survivor_total`, `pipeline_hypotheses_count`, `pipeline_survivors_count` | counters / histograms | Исходы LLM-пайплайна |
| `hypothesis_generated_total{perspective}`, `hypothesis_grounded_total{perspective}`, `hypothesis_refuted_total{by}`, `hypothesis_disagreement_total` | counters | Multi-hypothesis / FactCritic |
| `llm_request_duration_seconds{model}`, `llm_api_errors_total{model, error_type}` | histogram / counter | Латенси и ошибки LLM по backend-у (`app/core/tracing.py`) |
| `llm_tokens_per_agent_total{agent, model, direction}`, `llm_latency_per_agent_seconds{agent, model}`, `llm_errors_per_agent_total{agent, model, error_type}` | counter / histogram / counter | Расход по агентам |
| `llm_cost_usd_total{model}` | counter | Оценка расхода на LLM |
| `llm_budget_denied_total{reason}` | counter | Отказы предохранителя бюджета (потолок не задан / исчерпан / состояние счётчика неизвестно) |
| `prompt_input_truncated_total`, `prompt_input_chars` | counter / histogram | Обрезка входа промпта перед вызовом модели; размер входа до обрезки |

Всё, что под «pipeline», «hypothesis», «llm», «prompt», пишет только LLM-пайплайн. При `LLM_PIPELINE_ENABLED=false` в проде эти серии пустые; ровный ноль там — не сигнал здоровья. `llm_tokens_total{model, type}` объявлен в `app/observability/ai_metrics.py`, но его никто не инкрементирует (у хелпера `track_llm_metrics` нет вызовов).

### Алерты (`VMRule sre-ai-copilot`, `k8s/monitoring.yaml`)

| Алерт | Условие | Severity |
|---|---|---|
| `CopilotSelfHealthStale` | снимок старше 1 ч, в течение 15 м | warning |
| `CopilotSelfHealthAbsent` | нет `copilot_self_health_last_run_timestamp` 30 м | warning |
| `CopilotSelfHealthFailing` | любая проверка `>= 2` 30 м | warning |
| `CopilotSelfHealthWarnStuck` | любая проверка `== 1` 24 ч | warning (до 1.0.28 был info — его не постил ни один путь) |
| `CopilotApiDown` / `CopilotWorkerDown` / `CopilotWorkerSyncDown` | 0 доступных реплик `sre-ai-api` / `copilot-worker` / `copilot-worker-sync` 5 м | critical |
| `CopilotExecutorDown` | 0 доступных реплик `copilot-executor` 5 м | warning |
| `CopilotPodCrashLooping` | рестарты в ns `sre-ai` 15 м | warning |
| `CopilotPostgresBackupFailed` | упавший Job `postgres-backup-*` | warning |
| `CopilotPostgresBackupMissing` | нет успешного `postgres-backup` дольше 26 ч (или нет данных) | warning |
| `CopilotCeleryBacklog` | `max by (queue) (celery_queue_length) > 100` 15 м | warning |
