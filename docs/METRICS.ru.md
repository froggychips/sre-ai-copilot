# SRE AI Copilot — Metrics Pipeline

Обновлено: **2026-08-08**.

Карта потока метрик: какие сигналы есть в VictoriaMetrics, какие из них материализуются в Knowledge Graph, каким sync'ом, и — не менее важно — каких сигналов **нет** и что на самом деле означают их нули.

## 1. Карта сигналов (TL;DR)

| Сигнал | Таблица KG | Источник | Статус |
|---|---|---|---|
| `cpu_pct` / `mem_pct` / `restarts_rate` (per service) | `kg_service_health` | `kube_*` / cAdvisor через VMNodeScrape | ✅ live |
| `http_5xx_rate` / `p95_latency_ms` (per service) | `kg_service_health` | ASP.NET app `/metrics` | ❌ всегда 0 — блокировано WO-12483 |
| `p95` / `p99` / `rps` / `error_4xx_rate` / `error_5xx_rate` (per ingress endpoint) | `kg_ingress_observations` | `nginx_ingress_controller_*` через VMPodScrape | ✅ live с 2026-06-10 |
| Аномалии (robust-z) | `kg_anomaly_observations` | derived: `kg_service_health` + `kg_log_observations` | ✅ live |
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

### `kg_ingress_observations` ← `ingress_observations_sync.py` (beat: `kg_ingress_observations_sync`, ~10 мин)

Per `(ingress_name, host, path)`: `p95_latency_ms` / `p99_latency_ms` / `rps` / `error_4xx_rate` / `error_5xx_rate` из `nginx_ingress_controller_*`. Список host/path берётся из `kubectl get ingresses -A` (реюз парсера `k8s_ingress_sync`); backend-сервис резолвится в `service_id` через ingress backend → `kg_services` (namespace + name); если не зарезолвился — ряд всё равно пишется с `service_id = NULL`. Ряды, где все метрики 0, пропускаются (экспортёр не накрывает endpoint). Идемпотентность по `UNIQUE(ingress_name, host, path, ts)`.

Это **endpoint-уровневый** разрез (per host/path) — другой срез, чем per-service в `kg_service_health`. Сейчас это единственный источник реальных HTTP 5xx / latency в KG.

### `kg_anomaly_observations` ← `anomaly_detection.py` (beat: `kg_anomaly_detect`)

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
