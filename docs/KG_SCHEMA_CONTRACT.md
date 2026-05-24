# KG Schema / Quality Contract

> **Версия контракта:** `kg_schema: 2.1`
> **Дата:** 2026-05-24 (после merge PR #74-77 — Wave 7 / discord dedup)
> **Источник истины кода:** [`app/knowledge_graph/contract.py`](../app/knowledge_graph/contract.py)

Документ описывает что в Knowledge Graph считается **service**,
**orphan**, **synthetic**, **owner-known**, и каков перечень
допустимых **edge kinds** с семантикой. Используется как:

1. Якорь для quality dashboards (`/admin/kg/quality`, stats_digest):
   общий источник «orphan rate», «owner coverage».
2. Источник истины при добавлении новых волн (X / Y / Z / новые
   syncs) — что писать в `EDGE_KINDS`, как бампать версию.
3. Onboarding-документ: новый член команды читает `2.x` и понимает
   что значит каждый edge без чтения 30 sync-модулей.

---

## 1. Версия

```
KG_SCHEMA_VERSION = "2.1"
```

`major.minor`:

* **major** — breaking-change: edge kind переименован, схема одного из
  ядерных полей изменилась (`kg_services.namespace`, `kg_service_edges.kind`),
  semantic существующего kind перевёрнут. Требует миграции consumer'ов.
* **minor** — additive: новый edge kind, новый synthetic prefix, новые
  поля QUALITY_THRESHOLDS, перевод planned → active.

История:

| Версия | Дата | Что добавлено |
|---|---|---|
| 2.0 | Wave 6 (2026-05-16) | Базовый contract: `calls` / `uses_db` / `uses_nats`, synthetic flag, health_score |
| **2.1** | Wave 7 (2026-05-22) | + `serves_traffic`, `routes_to`, `pod_event_of`; `subject:` synthetic |
| 2.2 (planned) | PR #16 / #17 | + `runs_as_job`, `uses_volume`, `bound_to` |

---

## 2. Что такое service

**Service** = строка в `kg_services` (одна на пару `(namespace, name)`).

### 2.1. Real service

Создаётся `kg_sync` / `k8s_topology_resources_sync` когда в k8s есть:

* `Deployment` или
* `StatefulSet` или
* `DaemonSet`

Имя берётся из metadata.name. `team_owner` — см. секцию 5.
`synthetic = false`.

В кодовой константе: `REAL_SERVICE_KINDS = {"deployment", "statefulset", "daemonset"}`.

### 2.2. Synthetic service

`synthetic = true`. Создан парсером/sync-ом как **якорь зависимости**,
реального pod-а за ним нет. По дизайну никогда не имеет inbound pod
ownership, но **может иметь edges** (это и есть его смысл — быть
target'ом).

Префиксы имени (из `SYNTHETIC_KINDS`):

| Префикс | Кто создаёт | Назначение |
|---|---|---|
| `ingress:<host>` | `k8s_ingress_sync` | Точка входа извне; target `calls` / `serves_traffic` edge |
| `subject:<value>` | `nats_subjects_sync` (Wave 7-Z) | NATS subject; target `uses_nats` |
| `db:<driver>:<host>` | `kg_sync` (env_vars) | Backend БД; target `uses_db` |
| `nats` (имя без префикса, namespace=`nats`) | `kg_sync` | NATS-кластер целиком; target `uses_nats` |

**Исключаются из orphan-метрики**: synthetic-узлы по дизайну могут
быть без inbound edges (если никто их не использует — это валидное
состояние), либо без outbound (они terminal). Считать их orphan'ами
загрязняет показатель.

---

## 3. Orphan

**Orphan** = service, существующий в БД, но **не имеющий участия в
графе и без свежей активности**.

Формальное правило (см. `is_orphan()` в `contract.py`):

```
orphan(s) := NOT s.synthetic
         AND s.id NOT IN (SELECT src_id FROM kg_service_edges
                          UNION SELECT dst_id FROM kg_service_edges)
         AND NOT has_recent_deploy(s)  -- последние 30d
```

Параметр `has_recent_deploy` опциональный — текущая `stats_digest.kg_quality_section`
использует **только** edge-проверку (это исторический поведенческий
контракт; новые consumer'ы могут включать deploy-фильтр).

**Threshold**: `orphan_rate_max_pct = 10.0` (см. `QUALITY_THRESHOLDS`).
Превышение → warning в `STARTUP_CONTRACT_CHECK` логом.

---

## 4. Synthetic — почему исключаются из counts

`stats_digest` показывает orphan как `orphan / real_total`, где
`real_total = services_total - synthetic`. Причина:

* Synthetic-узлы по дизайну могут не иметь edges и не имеют pod-а.
* Включение их в знаменатель искажает «насколько ваши *реальные*
  workload-ы привязаны к графу».
* Synthetic = bookkeeping узел, не subject of operation.

Текущее field: `kg_services.synthetic` (Boolean, default=false).
Эвристика fallback (`is_synthetic` в `contract.py`) — по префиксу
имени, на случай legacy-rows без flag.

---

## 5. Owner-known

**Owner known** = `team_owner IS NOT NULL AND team_owner != ''
AND lower(team_owner) NOT IN {'unknown', 'n/a', '-', 'none'}`.

См. `owner_known()` в contract.py.

### 5.1. Источники owner-а (`OWNER_SOURCES`)

| Source | Кто проставляет | Приоритет |
|---|---|---|
| `manual` | admin endpoint (PR #19, pending) | 1 (override) |
| `k8s_labels` | `kg_sync` через `team-owner`/`owner` label | 2 |
| `namespace_prefix` | `_derive_team_owner()` — squad-N → squad-N | 3 (fallback) |
| `platform_static` | synthetic-узлы (ingress=external, db=data, nats=platform) | при создании |
| `suggested` | PR #18 (pending) — AI/heuristic suggestion, требует approve | 4 (только если ничего выше) |

Сейчас в схеме `kg_services` нет колонки `owner_source` — это backlog
на 2.2/3.0. Логика выбора уже в кодовых путях; константы зафиксированы
в `OWNER_SOURCES`.

### 5.2. Quality threshold

`owner_coverage_min_pct = 90.0` — ниже 90% общая coverage → warning.

---

## 6. Edge kinds inventory

Полный реестр — `app/knowledge_graph/contract.py` константой
`EDGE_KINDS`. Все active + planned:

| kind | src kinds | dst kinds | semantic | source (sync/parser) | example | status |
|---|---|---|---|---|---|---|
| `calls` | real | real ∪ ingress | Синхронный HTTP/gRPC | `kg_sync` (env_url_v2, env_vars, ingress) | town-service → world-service | active |
| `uses_db` | real | `db` | Read/write в БД | `kg_sync` (env_vars `*_CONN`/`*_DSN`) | town-service → db:postgres:postgres-squad-1 | active |
| `uses_nats` | real | `nats` ∪ `subject` | NATS pub/sub | `kg_sync` (nats_env) + `nats_subjects_sync` | town-service → subject:march-export | active |
| `serves_traffic` | real | `ingress` | Принимает HTTP-трафик через ingress | `k8s_topology_resources_sync` (Wave 7-X) | wo-api-squad-1 → ingress:wo-api-squad-1.lastoasisgame.com | active |
| `routes_to` | `ingress` | real | Ingress правило роутит на backend | `k8s_topology_resources_sync` (Wave 7-X) | ingress:wo-api.* → wo-api-squad-1 | active |
| `pod_event_of` | real | real | Pod event linked к сервису (через FK, не отдельный edge — для документации) | `runtime_correlation` (Wave 7-Y) | kg_pod_events.service_id → kg_services.id | active |
| `runs_as_job` | real | real | Service запускается как k8s Job/CronJob | `k8s_jobs_sync` | backup-cron-town → (self, schedule) | **planned (PR #16)** |
| `uses_volume` | real | real | Service монтирует PV/PVC | `k8s_storage_sync` | postgres-squad-1 → pvc:data-postgres-squad-1-0 | **planned (PR #17)** |
| `bound_to` | real | real | PVC bound к PV (cluster-PV резерв) | `k8s_storage_sync` | pvc:data-postgres-squad-1-0 → pv:pvc-abc123 | **planned (PR #17)** |

Каждый kind должен:

1. Появиться в `EDGE_KINDS` с `status='planned'` **до** merge wave-а.
2. Переключиться в `'active'` одновременно с merge.
3. Бампнуть `KG_SCHEMA_VERSION` (см. §8).

---

## 7. Quality metrics

`QUALITY_THRESHOLDS` фиксирует что считается «good KG»:

| Метрика | Threshold | Direction | Где считается |
|---|---|---|---|
| `orphan_rate_max_pct` | 10.0 | ≤ | `stats_digest.kg_quality_section` |
| `owner_coverage_min_pct` | 90.0 | ≥ | `stats_digest` (planned) |
| `sha_coverage_min_pct` | 50.0 | ≥ | KG DQ audit (см. memory `project_kg_dq_audit_2026_05_22`) |
| `deploy_attribution_min_pct` | 50.0 | ≥ | per-service: ≥1 deploy за 30d |
| `synthetic_share_max_pct` | 40.0 | ≤ | если synthetic >40% — sync переусердствовал |

Все значения экспортируются из `contract.QUALITY_THRESHOLDS` —
дашборд должен импортировать константу, а не зашивать число.

Baseline (на момент 2026-05-22, см. memory `project_kg_snapshot_2026_05_22`):

* services real: 364
* team_owner coverage: 92% (✓)
* orphan rate: ~16% (✗ — выше threshold; работа в progress)
* sha coverage: 44.8% (✗)

---

## 8. Compatibility policy

### 8.1. Добавление нового edge kind

1. Открываем PR с syncs/parser, который создаёт edge.
2. **В том же PR**: добавляем запись в `EDGE_KINDS` с `status='planned'`.
3. После merge: отдельным PR (или squash) переводим `status='active'`,
   бампаем `KG_SCHEMA_VERSION` (minor).
4. `STARTUP_CONTRACT_CHECK` логирует warning если planned kind уже
   встречается в БД — это сигнал перевести в active.

### 8.2. Deprecation

1. Помечаем `status='deprecated'` в `EDGE_KINDS` (новое значение,
   добавить при необходимости).
2. Sync перестаёт писать новые ребра этого kind.
3. После окна grace (30d) — `drift_cleanup` удаляет stale ребра.
4. Удаляем запись из `EDGE_KINDS`, бампаем major version.

### 8.3. Version bump rules

| Изменение | Bump |
|---|---|
| Новый edge kind, новый synthetic prefix | minor (2.1 → 2.2) |
| Новый QUALITY_THRESHOLD | minor |
| Изменение semantic существующего kind | major (2.x → 3.0) |
| Удаление kind | major |
| Renaming таблиц / breaking schema | major |

### 8.4. STARTUP_CONTRACT_CHECK

Запускается при boot копилота (вызов из `app/main.py` lifespan или
worker startup). Сверяет:

* Все kinds в БД присутствуют в `EDGE_KINDS` (иначе warning
  `unknown_edge_kinds_in_db`).
* `planned` kinds не должны встречаться в БД (если встретились —
  warning, signal к переключению на active).
* Текущие `orphan_pct` / `owner_pct` против threshold-ов.

Не throws, не блокирует boot. Это диагностический инструмент,
а не gate.

---

## 9. Применение в кодовой базе

Файлы, которые **должны** импортировать константы отсюда:

* `app/services/stats_digest.py` — `is_orphan`, `is_synthetic`,
  `QUALITY_THRESHOLDS` для orphan/owner pct.
* `app/knowledge_graph/health_score.py` — `is_synthetic` (чтобы
  скипать synthetic из health-compute).
* `app/knowledge_graph/drift_cleanup.py` — `EDGE_STALE_WINDOW_DAYS`,
  `EDGE_KINDS` для валидации.
* Новые wave'ы (sync/parser) — `EDGE_KINDS` для писательного контракта
  (kind должен быть в реестре до того как уйдёт в БД).

Категорически **нельзя** хардкодить:

* Литералы edge kinds (`"calls"`, `"uses_db"`...) — импортируем ключи
  из `EDGE_KINDS`.
* Synthetic-префиксы — импортируем `SYNTHETIC_KINDS`.
* Threshold-числа orphan / owner — `QUALITY_THRESHOLDS[...]`.

---

## 10. См. также

* [`docs/SEMANTIC_CONTRACT.md`](SEMANTIC_CONTRACT.md) — incident lifecycle / async jobs / feedback.
* [`app/knowledge_graph/schema.py`](../app/knowledge_graph/schema.py) — SQLAlchemy ORM (источник истины для колонок).
* [`app/knowledge_graph/contract.py`](../app/knowledge_graph/contract.py) — исполнимая версия этого документа.
