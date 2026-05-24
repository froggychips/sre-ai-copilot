# KG Quality Report — baseline 2026-05-24

> **Цель документа.** Точка отсчёта перед Phase A (remediation). После
> merge 17 PR за 2026-05-24 (Wave 7 X/Y/Z + storage + jobs + owner inference
> multi-signal + stale_class column + contract v2.1 + Discord UX) KG
> наполнен новыми сигналами. Phase A будет сравнивать свои метрики
> относительно этого snapshot'а — diff = improvement.
>
> Файл собирается скриптом `app/scripts/quality_report.py` (read-only,
> ничего не пишет в БД). Snapshot ниже — **placeholder-структура**.
> Реальные числа заполняются прогоном против prod-PG (см. секцию
> «Как запустить» внизу).

Generated (placeholder): `2026-05-24T00:00:00`
Schema contract version: `2.1` (см. `app/knowledge_graph/contract.py`)

---

## 1. Service ownership

| Метрика | Значение |
|---|---|
| Total services (real) | `???` |
| Total services (synthetic) | `???` |
| Total services (всего) | `???` |
| owner_known | `???/??? = ???%` |

### Owner sources breakdown

| owner_source | count |
|---|---|
| manual | `???` |
| k8s_labels | `???` |
| namespace_prefix | `???` |
| platform_static | `???` |
| suggested | `???` |

> Breakdown берётся из `kg_services.metadata_json.owner_source`. Если
> поле ещё не материализовано (owner inference syncs его не пишут как
> metadata-key) — секция будет пустой, а скрипт выведет note об этом.
> Phase A знает что заполнение этого поля — одна из её задач.

---

## 2. Topology coverage

### Edges (kg_service_edges) by kind

| kind | count |
|---|---|
| calls | `???` |
| uses_db | `???` |
| uses_nats | `???` |
| serves_traffic | `???` |
| routes_to | `???` |
| pod_event_of | `???` |

### Jobs sync linkage (#82)

| Метрика | Значение |
|---|---|
| kg_k8s_jobs total | `???` |
| jobs linked to service | `???/??? = ???%` |

### Volume edges (kg_volume_edges, #84)

| kind | count |
|---|---|
| uses_volume | `???` |
| bound_to | `???` |

### Storage volumes (kg_storage_volumes, #84)

| kind | count |
|---|---|
| pvc | `???` |
| pv | `???` |

### Orphans (среди real-сервисов)

| Метрика | Значение |
|---|---|
| без HTTP-edges (calls/serves_traffic/routes_to) | `???` |
| без NATS-edges (uses_nats) | `???` |

---

## 3. Stale classification (#86)

| Метрика | Значение |
|---|---|
| active | `???` |
| expected_stale | `???` |
| suspicious_stale | `???` |
| stale_class IS NULL (не пересчитано) | `???` |

> `stale_class IS NULL` — индикатор того что `kg_sync.sync_namespace`
> для namespace ещё не вызывался после merge #86 (новый column).

### Top-10 suspicious_stale

| namespace | name | team_owner | last_deploy_at |
|---|---|---|---|
| `???` | `???` | `???` | `???` |

---

## 4. Alert enrichment quality (last 24h)

| Метрика | Значение |
|---|---|
| Total alerts (24h) | `???` |
| with linked service | `???/??? = ???%` |
| with owner | `???/??? = ???%` |
| with blast_radius (serves_traffic/routes_to IN-edges) | `???/??? = ???%` |
| with NATS impact (uses_nats edges) | `???/??? = ???%` |
| with pod_trail (kg_pod_events ±60м) | `???/??? = ???%` |

> Важно: `kg_alerts` не хранит `hypothesis_text` / `likely_cause` —
> enrichment строится в `alert_enrichment.py` по запросу. Метрики выше —
> это **потенциал enrichment-а**: какой fraction alert-ов имеет такую
> структуру в KG (FK на service, edges нужного kind), что соответствующая
> секция в Discord embed появилась бы.
>
> Phase A улучшит:
> 1. attribution `alert.service_id` (сейчас target_resolve может не
>    сматчить → service_id=NULL);
> 2. owner-резолв (если service есть, но team_owner пуст);
> 3. blast/nats покрытие за счёт более полного KG.

---

## 5. Deploy attribution (last 30d)

| Метрика | Значение |
|---|---|
| Total deploys (30d) | `???` |
| linked to service | `???/??? = ???%` |
| with commit_sha | `???/??? = ???%` |

> Per память «KG DQ-аудит 2026-05-22»: на момент аудита sha=0/3901
> (0% sha coverage). Phase A — главная цель здесь именно sha-backfill
> через TC API + связать deploy с service (`kg_deploys.service_id`).

---

## Как запустить (для заполнения placeholder'ов)

### Локально (если поднята dev-БД)

```bash
cd ~/sre-ai-copilot
.venv/bin/python -m app.scripts.quality_report > docs/quality_report_baseline_2026_05_24.md
```

### Прод (sre-ai namespace в prod-k5)

```bash
# через api pod (имеет DATABASE_URL в env)
kubectl -n sre-ai exec deploy/sre-ai-api -- \
    python -m app.scripts.quality_report > /tmp/baseline.md

# скачать
kubectl -n sre-ai cp $(kubectl -n sre-ai get pod -l app=sre-ai-api -o name | head -1 | cut -d/ -f2):/tmp/baseline.md \
    docs/quality_report_baseline_2026_05_24.md
```

### JSON (для diff-tooling в Phase A)

```bash
python -m app.scripts.quality_report --json --output baseline-2026-05-24.json
```

---

## Как Phase A использует этот snapshot

1. Перед каждой fixing-волной (owner backfill / deploy sha-backfill /
   namespace owner sync) — генерим новый JSON dump.
2. Diff против `baseline-2026-05-24.json` показывает delta cell-к-cell:
   `owner_known_pct: 60.0 → 87.4 (+27.4%)`.
3. Регрессии (метрика просела) — выявляются тем же diff'ом и
   попадают в weekly-стенд через `stats_digest`.
4. Schema contract version (см. `contract.KG_SCHEMA_VERSION`) bump'ается
   когда меняется набор метрик; baseline пересоздаётся с нуля.

---

## Замечания (см. `app/scripts/quality_report.py` для деталей)

* Скрипт **read-only** — никаких INSERT/UPDATE/DELETE.
* Synthetic-сервисы исключаются из `services_total_real` через
  фильтр (`synthetic=true` ИЛИ имя `ingress:` / `subject:` / `db:` /
  `nats:`). Логика дублирует `contract.is_synthetic` SQL-side.
* `owner_known_pct` совпадает с метрикой `STARTUP_CONTRACT_CHECK.owner_pct`
  по семантике, но знаменатель здесь — `services_total_real`, а в
  contract-check — `services_total` (включая synthetic). Это сознательное
  расхождение: для baseline'а интереснее реальные пробелы.
* `with_pod_trail` считается со скольжением ±60м от `alert.fired_at` —
  совпадает с window-ом в `queries.pod_event_summary_for`.
