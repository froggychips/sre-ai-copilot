# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

### Добавлено

- **Golden-набор и офлайн-eval** (`tests/golden/`, `scripts/eval_golden.py`):
  20 зафиксированных инцидентов с ожиданиями и метрики, которым CI не даёт
  просесть. До этого качество разбора — единственное, ради чего продукт
  существует — проверялось шестью ручными прогонами в README и 👍/👎 в
  Discord, при том что ruff/mypy/bandit/pip-audit/coverage/KG-contract уже
  были автоматическими.
  * режим `replay` (в CI на каждом PR): записанные LLM-ответы, без сети и
    ключа. Меряет детерминированный обвес — правила фактов, anchor-грounding,
    отбор гипотезы, политику executor-гейта. Именно там жили регрессии
    combat runs 1–3, теперь это кейсы `001` и `013`;
  * режим `live` (`.github/workflows/eval-live.yml`, воскресенье + вручную):
    реальная модель, меряет качество текущих промптов, ветку не блокирует;
  * `--check-baseline` сверяет метрики покейсно и по группам проверок —
    без разреза новые кейсы на факты маскировали бы просадку на гейте.
- **Deadman-канарейка CI** (`scripts/ci_deadman.py`, `k8s/ci-deadman.yaml`):
  CronJob в кластере (не workflow — тот умирает вместе с наблюдаемым
  раннером) следит за единственным self-hosted раннером, застрявшей очередью,
  зависшими зелёными PR-ами и красным master. Первый прогон нашёл 5 висящих
  dependabot-PR.

### Изменено

- **Доставка Discord-отчёта вынесена из `IncidentPipeline`** в
  `app/workers/report_delivery.py` (`ReportDelivery`): outbox-маркеры, счётчик
  попыток, severity-gate и решение «ретраить или сдаться» больше не соседствуют
  с машиной состояний. `pipeline.py` 1742 → 1468 строк; механика доставки
  тестируется на `record` + `db` без моков восьми агентов (22 новых теста).
  Поведение не менялось.
- `.mailmap`: четыре git-идентичности одного автора схлопнуты в одну.

## [1.0.0-rc.15] — 2026-08-10 — Дайджесты на AUTOCOMMIT-сессии

Финал разбора «дайджест молчал два дня» (08–09.08): после снятия OOM-петли
(rc.14) `daily_stats_digest` дожил до исполнения и умер на ~170-й секунде —
«server closed the connection unexpectedly». Сборка дайджеста перемежает SQL
с минутами VM/Discord I/O, и обычная сессия висела в `idle in transaction`
дольше 120с (лимит из rc.11) — PG рвал соединение. Это и был первый убийца
дайджеста; OOM-петля — второй, параллельный.

### Исправлено

- **`daily_stats_digest` и `chronic_alerts_digest` работают через
  `ReadOnlyAutocommitSession`** (`isolation_level="AUTOCOMMIT"`, PR #252):
  транзакция закрывается после каждого statement — idle-in-transaction
  исчезает как класс, ACCESS SHARE-локи не копятся, DDL не блокируется.
  По PG оба таска read-only.

## [1.0.0-rc.14] — 2026-08-10 — Дедлоки kg_services + смерть kg_metrics_sync

По следам пропавшего дайджеста: 594 deadlock/сутки на `kg_services`,
`kg_metrics_sync` мёртв 18 часов, воркеры в OOM-петле каждые ~1.5–2ч.

### Исправлено

- **`recompute_all_health`: commit батчами по 100 + `ORDER BY id`** (PR #251).
  При `autoflush=False` единственный commit в конце флашил ~9k UPDATE-ов
  одной транзакцией — десятки секунд накопления row-локов в произвольном
  порядке; встречные ON CONFLICT-upsert-ы синка топологии ловили deadlock.
- **`metrics_sync`: read-транзакция закрывается до fetch-фазы** (PR #251).
  `db.query(Service)` открывал транзакцию, тик уходил в многоминутный
  VM-fetch, PG убивал соединение по `idle_in_transaction_session_timeout` —
  синк не завершался ни разу с 09.08 12:50.
- **`k8s/worker.yaml`: memory 768Mi → 3Gi** (requests==limits, Guaranteed).
  Live-кластер дополнительно выровнен: cpu 600m (был дрейф 200m — воркеры
  CPU-затроттлены на 91–98% периодов, все таски растягивались в разы).

## [1.0.0-rc.13] — 2026-08-08 — Хвост глубокого ревью (rc.12 — промежуточная сборка волны)

### Добавлено

- **Секция «KG через MCP» в дайджесте** — сколько людей реально пользуются
  графом (PR #244).
- **NATS-рёбра для squad- и QA-стендов** — корень низкой связности графа
  (PR #243).

### Исправлено

- **`kg_ingress_observations_sync`: 5 запросов к VM вместо ~5000 за тик**
  (PR #242) — закрыта «известная проблема» rc.11.
- **Дрейф репозиторий ↔ кластер: registry, RBAC, lock_timeout** (PR #241) —
  закрыта вторая «известная проблема» rc.11.
- **`kg_sync` ссылался на переименованный constraint** — синк падал на всех
  ns (PR #245).
- **Хвост глубокого ревью**: blocking I/O, decay, IDOR, `acks_late` +
  `reject_on_worker_lost` + `prefetch=1`, токен в БД (PR #237); висящие
  транзакции + честный orphan после выката `node_kind` (PR #238); батчевый
  commit в синке топологии (PR #240).

## [1.0.0-rc.11] — 2026-08-08 — Типы узлов KG, честные метрики, безопасные миграции

Главное: узел графа перестал означать одновременно k8s Service и workload.
Побочно вскрылись и закрыты три проблемы эксплуатации, из-за которых часть
синков жила по остаточному принципу.

Полный разбор, включая допущенные промахи —
[`docs/POSTMORTEM_2026_08_08.ru.md`](docs/POSTMORTEM_2026_08_08.ru.md).

### Добавлено

- **`kg_services.node_kind`** (`service` / `workload` / `ingress`), уникальный
  ключ расширен до `(namespace, name, node_kind)` (contract 2.4, PR #233).
  Service `auth` и Deployment `auth` были ОДНОЙ строкой, поэтому ребро
  `serves_traffic` не могло существовать: оно вырождалось в self-loop и
  отбрасывалось. Замер на живом графе — 2092 выброшенных ребра за тик синка
  при 3 уцелевших в графе.
- **Матчинг workload'ов расширен на StatefulSet и DaemonSet.** Раньше
  матчились только Deployment'ы, из-за чего 2231 Service за тик уходил в
  `skipped_no_match` — это все `*-db` / `*-postgresql` / clickhouse.
- **`lock_timeout` в Job-е миграций** (`PGOPTIONS`). DDL в очереди блокирует
  всех читателей, пришедших после него.
- **`expires` у всех 27 периодических beat-задач** (PR #239). Был ровно у
  одной.
- **Постмортем и процедура выката** в RUNBOOK (ru/en): миграции под
  блокировками, диагностика локов, разовый запуск тяжёлых задач, проверка
  RBAC.

### Изменено

- **`orphan_pct` больше не засчитывает `serves_traffic` как связность**
  (contract 2.5, PR #238). Это ребро на собственный workload узла; его учёт
  уронил orphan с 72.5% до 42.0% без единой новой интеграции — артефакт
  схемы, а не улучшение топологии.
- **Метрики качества считают только `node_kind='service'`.** Иначе 2871
  workload-узел удвоил бы знаменатель и обвалил owner-coverage с 99.5% до
  ~50% как ложную регрессию.
- **CI гоняет весь набор против живого Postgres** плюс `alembic upgrade head`
  (PR #234). Раньше — только два интеграционных файла, потому что при
  `DATABASE_URL` включались 45 никогда не исполнявшихся тестов.
- **Синк топологии коммитит батчами** по 200 записей. Одна транзакция на
  4200+ upsert-ов жила 12-13 минут и всё это время держала ACCESS SHARE,
  из-за чего не проходила миграция.

### Исправлено

- **21 место резолвило узел по `(namespace, name)` без типа**, восемь из них
  через `.one_or_none()` на горячем пути (обогащение алертов, blast-radius,
  Discord-эмбеды, remediation) — с появлением одноимённого workload дали бы
  `MultipleResultsFound` ровно в момент инцидента.
- **45 тестов, падавших при живой БД** (PR #234). Корень у 40 из них один:
  dedup стал cross-replica (таблица `discord_dedup`), а тесты чистили только
  in-memory кэш.
- **Находки code scanning** (PR #236): `permissions` в двух workflow, утечка
  текста исключения в Discord-ответ.
- **Откат миграции `node_kind`** падал на FK `kg_service_edges_dst_id_fkey`;
  теперь снимает ссылки перед удалением узлов.

### Известные хвосты

- Дрейф репозиторий ↔ кластер: `deploy.sh` деплоит из `ghcr.io`, а кластер
  тянет из Nexus; `ClusterRole sre-ai-read` из репозитория в кластер никогда
  не применялась.
- `kg_ingress_observations_sync` шлёт тысячи запросов в VictoriaMetrics и
  занимает воркеров надолго.

## [1.0.0-rc.3] — 2026-06-26 — KG data-quality gate (drift watchdog)

Закрывает главную слабость, вскрытую в rc.2: «build обгонял validate-against-
reality» — структурный дрейф графа копился незамеченным до ручного аудита.
Теперь дрейф ловится автоматически.

### Knowledge Graph — self-health

- **`graph_integrity` check в KG self-health (#7).** Добавлен в существующий
  beat-watchdog `kg_self_health_check` (каждые 30 мин, audit-log + Discord-embed
  на FAIL в self-health канал, 6h-dedup). Проверяет структурные инварианты,
  которые ДОЛЖНЫ быть 0 по построению, как regression-watch для багов rc.2:
  - `db_phantom_dup_names` — `db:%`-узлы в >1 namespace (#185/#189) → fail.
  - `serves_traffic_self_loops` / `self_loops_any` — петли (#190) → fail.
  - `dangling_edges` — рёбра с отсутствующим src/dst → warn (>0) / fail (>50).
  Read-only, ноль новой инфры — переиспользует beat-расписание, Discord и dedup
  существующего self-health canary.

Проход по качеству графа после rc.1. Две структурные дыры закрыты на уровне
кода И вычищены на живом KG; «в стол» собиравшийся endpoint-RED-сигнал
подключён в инцидент-пайплайн. Полный сьют: **1617 passed, 12 skipped**.
Версии сведены: non-prod FastAPI app `2.4.0` → `1.0.0-rc.2` (был дрейф с prod).

### Knowledge Graph — целостность графа

- **Фантомные db-узлы схлопнуты (#189, C2).** До #185 `secret_hint` строил
  `db:<driver>:<host>`-узел в OWN namespace → один физический кластер БД
  размножался в per-namespace копии (замер: 16 реальных БД → **288 узлов**,
  до 24 копий одной). Backfill-модуль `phantom_db_cleanup` (dry-run по
  умолчанию, savepoint per-name, GREATEST weight, идемпотентно) +
  CLI `cleanup_phantom_db_nodes`. На живом KG: **288 → 16** узлов, 272
  удалено, 256 рёбер перенаправлено, 887 слито, висячих рёбер 0.

- **`serves_traffic` self-loops убраны (#190).** Граф ключует `kg_services` по
  `(name, namespace)` без разделителя типа → одноимённые k8s Service и
  Deployment это один узел, а `serves_traffic`-ребро между ними — петля.
  Замер: **2215 петель ≈ 35% всех рёбер**, они засоряли blast-radius (сервис
  показывал сам себя в «кто пострадает»). Guard `src==dst→skip` в
  `k8s_topology_resources_sync` + backfill-модуль/CLI. На живом KG: **2215 → 0**.

### Knowledge Graph — сигнал

- **Ingress-derived HTTP RED подключён (B2).** `kg_ingress_observations`
  (nginx-ingress per host/path 5xx/p95/rps, ~120k строк, 501 сервис) собиралась
  с 2026-06-10, но **не потреблялась** (per-service `kg_service_health.http_5xx`
  всегда 0 — app `/metrics` за JWT, WO-12483). Добавлены: `queries
  .ingress_health_for`, компонент `ingress_5xx_penalty` в health-score, секция
  «🌐 Endpoint health (ingress-derived)» в critical Discord-embed. Явно помечен
  ingress-разрезом, НЕ пишется в `kg_service_health` (без «фальшивого зелёного»).
  Per-service вариант остаётся блокером инфры WO-12483.

## [1.0.0-rc.1] — 2026-06-25 — Security & reliability hardening (release candidate)

Крупнейший проход харднинга за историю проекта: ~22 фикса по итогам внутреннего
глубокого код-ревью. Закрыт весь CRITICAL/P0-тир; opt-in executor сделан реально
безопасным (решение «можно ли применить kubectl» больше не отдано LLM). Полный
сьют на финальном master: **1591 passed, 12 skipped**.

> RC перед финальным 1.0.0. Дефолты не изменились (advisory-режим, executor за
> opt-in флагами). Версии сведены: FastAPI app `2.4.0` → `1.0.0-rc.1`.

### Executor / approval — безопасность реального write

- **Детерминированный policy-gate вместо LLM-`risk` (#165, CRITICAL).** Раньше
  единственным гейтом перед `kubectl --dry-run=false` была строка `intent.risk`
  из ответа `FixAgent` — её можно было занизить prompt-injection'ом, а весь
  `app/remediation/*` (8 risk axes, policy evaluator) был мёртвым кодом. Теперь
  `apply_intent` пересчитывает риск из *структурного* intent-а
  (`app/remediation/executor_gate.py`) и блокирует prod/system/data-plane/
  необратимое; LLM-`risk` — лишь advisory. Попутно закрыт exact-match
  `READ_ONLY_NAMESPACES` (теперь префиксная классификация — `prod-*` ловится).
- **Подпись на apply-пути + проверка `ActionApproval` (#167, CRITICAL+HIGH).**
  `apply_confirm` теперь несёт подпись intent-а в `custom_id` (TOCTOU-сверка), а
  `apply_intent` перед write требует совпадения подписи **и** записи
  `ActionApproval` (approved) для инцидента. `post_approval=True` (прежний обход
  SAFE_MODE) больше не достаточен сам по себе.
- **Anti-replay по timestamp (#166, CRITICAL).** Discord-interactions (Ed25519) и
  AlertManager (HMAC) проверяют свежесть timestamp
  (`DISCORD_INTERACTION_MAX_AGE_SECONDS`, `ALERTMANAGER_WEBHOOK_MAX_AGE_SECONDS`,
  дефолт 300с; `ALERTMANAGER_REQUIRE_SIGNED_TIMESTAMP` для enforce). Перехваченный
  подписанный apply/approve-клик больше нельзя переиграть.
- **Row-lock идемпотентность `apply_intent` (#168, M3).** `SELECT … FOR UPDATE` на
  инциденте — double-click / реплей больше не выполняют `kubectl` дважды.
- **`/replay` RBAC + SSRF-allowlist (#186).** `require_role("approver")` на
  replay-ручках; `validate_snapshot_uri` (allowlist `s3://`/`file://`); попутно
  фикс latent routing-бага `/by-snapshot` и утечки сессии.

### Надёжность / стоимость

- **Retry-шторм LLM (#170, CRITICAL).** SDK `max_retries=0` + client-timeout +
  сужённый retry-предикат (только 5xx/429/сеть/таймаут) — вместо до 9× HTTP на
  один agent-вызов и «зомби»-запросов после `asyncio.wait_for`.
- **AM-down больше не гасит ложно живые алерты (#169, CRITICAL).** Age-fallback
  в `alerts_resolve_sync` резолвит только при подтверждённом живом AM-снимке;
  при AM down проход пропускается (иначе долгоживущие firing critical
  — etcd/disk/prod-down — ложно «гасли» в Discord).
- **Дедуп incident-канала → cross-replica PG (#171, CRITICAL).** `_post_or_edit_incident`
  переведён с per-process dict на общий `dedup_store` — на 2+ репликах api больше
  нет дубль-POST critical с повторным `@here`.
- **Savepoint-изоляция KG-синхронизаторов (#175, H2).** `db.begin_nested()` per-item
  в `auto_populator`/`k8s_events_sync`/`seq_logs_sync`/`metrics_sync` — один битый
  ряд больше не отравляет сессию и не валит весь tick.
- **Единый Celery-app (#174, C2).** Два `Celery()` на одном Redis сведены к одному;
  пользовательский `generate_reply` наследует backpressure-конфиг.
- **stop_reason / удаление мёртвого llm_cache (#182).** Обрезка по `max_tokens`
  теперь видна (`truncated`-флаг), а не маскируется под пустой ответ; dead-code
  `llm_cache.py` удалён + честный комментарий.

### Корректность / данные / контекст

- **prompt_guard не блокирует легит (#176, H4).** Длинный ввод обрезается (не
  отклоняется), код-паттерны (`import os`/`eval(`) больше не блокируются —
  крэш-трейсбэки их легитимно содержат; injection-паттерны блокируются как прежде.
- **VM None-sentinel (#180, H1).** `query_instant` возвращает `None` (не `0.0`) при
  сбое — частичный сбой метрик больше не маскируется под «здоровый» кластер;
  `data_available`/`degraded` учитывают отсутствие данных.
- **RCAExplainer fail-safe (#178, H3).** `.get()` вместо жёстких индексов, маппинг
  действий по стабильному `kind`, неузнанная причина → `approval_required=True`
  (раньше дрейф имени делал OOM-фикс auto-approvable).
- **pod_event service_id backfill (#177, H3).** Атрибуция до-проставляется при
  позднем появлении сервиса — OOM/CrashLoop больше не остаются orphan.
- **KG H1 — деплои не теряются (#184).** Непарсящийся `finished_at` больше не
  роняет build через `None.replace()`.
- **kg_sync: weight не понижается + фильтр фантомных db-узлов (#185, H5/C2).**
  `GREATEST(existing, excluded)` для weight; secret-hint db-узлы сверяются с
  реестром реальных, без `unverified_host` помечаются для фильтрации.
- **Seq `count_events` реальный total (#179, H4).** Пагинация по `afterId` вместо
  `count=1` → error-rate сигнал больше не мёртв.
- **`_peer_namespace` N+1 (#172, C1).** Дифф-диагностика сравнивает сквад с
  реальным соседом, а не с хардкод-`squad-2`.
- **socket.setdefaulttimeout убран (#173, C2).** Per-call `_request_timeout` вместо
  процесс-глобальной мутации, рвавшей таймауты unrelated клиентов под concurrency.

### Безопасность данных / инфра

- **PII-редакция: AWS (`AKIA`/`ASIA`), Basic-auth, PEM private-key (#181).**
- **Пул БД (#183, H3):** явные `pool_size/max_overflow/pool_timeout/pool_recycle`
  (дефолтный потолок 15 не держал threadpool + Celery); psycopg2-leak в
  `statics_service` (close в `finally`); `dedup_store.get_fresh` больше не делает
  `DELETE+COMMIT` в hot-path — purge вынесен в beat-задачу.

## [0.14.0] — 2026-06-23 — Alert-quality: подавление шума + ops-харднинг

Серия алерт-качества: два новых класса подавления шума на этапе render-а (НЕ
дроп и НЕ демот severity — карточка остаётся видимой, но приглушается: grey +
🔇, без 🚨/@mention), плюс операционные фиксы (beat OOM, объявление first-party
зависимостей) и наполнение `kg_ingress_observations`.

### Подавление шума на этапе render (alert-quality)

- **`meta_noise` — мета-агрегаты и scrape-gap (PR #154, прецедент
  ProdNewCriticalAlerts 2026-06-16)**: класс по образцу `rollout_noise`, но
  шумящий ВСЕГДА, не в окне деплоя. Ловит `*NewCriticalAlerts`
  (Prod/Preprod/Squad) — агрегат-счётчик, дублирующий сигнал (каждый реальный
  критикал и так приходит отдельной громкой карточкой со своим
  сервисом/деплоем/KG) — и производные control-plane scrape-gap
  (`etcdInsufficientMembers` / `ScrapePoolHasNoTargets` /
  `RecordingRulesNoData`). `META_NOISE_ENABLED` kill-switch; `_detect_meta_noise`
  + флаг `meta_noise` на `EnrichedContext` (выставляется в `enrich_alert`);
  muted-рендер в `discord/service.py` (тег 🔇 META-AGGREGATE). 12 тестов
  (`tests/test_meta_noise_suppression.py`).
- **`gen_mismatch_noise` — условный churn `KubeDeploymentGenerationMismatch`
  (PR #160, прецедент prod-kingdom7/town-service 2026-06-23)**: в отличие от
  meta-noise шумит НЕ всегда. `metadata.generation != observedGeneration`
  штатно флапает, когда внешний контроллер (Rancher/cattle-cluster-agent
  дописывает publicEndpoints-аннотацию) бьёт generation, а deployment-
  контроллер на миг отстаёт — накат давно сошёлся. Тот же alertname, однако,
  сигналит и реальный зависший накат. Различитель — здоровье реплик
  (`ctx.replicas_ready_desired`): `ready==desired (≥1)` → приглушаем (🔇
  GENERATION-CHURN); `ready<desired` / `?/N` / `None` / `0/0` → **fail-safe
  LOUD** (реальный фейл звенит в любом ns, включая prod). Health-gating >
  namespace-скоуп. Не перекрывает `rollout_noise` (тот глушит deploy-window
  кейс) — этот ловит no-deploy churn. `GEN_MISMATCH_NOISE_ENABLED` kill-switch.
  14 тестов (`tests/test_gen_mismatch_noise.py`).

### Операционка / зависимости

- **`copilot-beat` memory 128Mi→256Mi (OOMKilled крашлуп)** — `k8s/worker.yaml`.
- **Объявлены first-party зависимости `cryptography==48.0.1` +
  `pydantic-settings==2.14.2` (PR #161/#162)** — импортились, но не были в
  `requirements.in` → красный CI на dependabot-PR (CI ставит только
  `requirements.txt`); durable-фикс закрыл дрейф `requirements.in` vs Dockerfile
  и CVE.
- **Bumps**: anthropic 0.107→0.111, fastapi 0.136→0.138, sqlalchemy 2.0.50→
  2.0.51, kubernetes 31.0.0→36.0.2, starlette (pip group), actions/checkout 6→7.

### KG coverage — ingress-метрики live (2026-06-10)

- **nginx-ingress метрики включены на кластере**: `--enable-metrics=true` на
  обоих контроллерах WO (shared `ingress-nginx-controller`, 16 нод +
  `ingress-prod-controller`, 5 prod-нод); per-host лейблы сохранены
  (`metrics-per-host` по умолчанию true — выключать нельзя, sync фильтрует
  по host). Скрейп: VMPodScrape `ingress-nginx-shared` / `ingress-prod`
  в ns cattle-system с `honorLabels: true` (иначе `namespace` перетирается
  в `exported_namespace`), VMAgent в ns monitoring (`selectAllByDefault`).
- **`kg_ingress_observations` наполняется**: beat `kg_ingress_observations_sync`
  каждые ~10 мин пишет per-host/path p95/p99/rps/4xx/5xx. Первые 50 минут:
  156 рядов, 23 хоста, все контуры (prod/squad/preprod/preupdate/infra),
  100% рядов слинкованы с `kg_services`.
- **Доки и код-комменты про scrape-gap обновлены** под новое состояние:
  `app/knowledge_graph/{metrics_sync,health_score,anomaly_detection,queries}.py`,
  `docs/ARCHITECTURE.md` / `ARCHITECTURE.ru.md`, README.
- **Per-service `http_5xx_rate`/`p95_latency_ms` в `kg_service_health`
  по-прежнему всегда 0** — `/metrics` ASP.NET-сервисов (Kestrel) закрыт
  JWT-middleware (401); ждут бэкенд-тикета WO-12483 (отдельный
  management-порт без auth), после раскатки добавится VMServiceScrape на
  app-ns. До тех пор `health_score` — инфра-прокси, `log_error_rate` —
  лог-прокси, не HTTP 5xx.

## [0.13.0] — 2026-06-10 — Security Hardening + Remediation Phase A

Три недели после Wave 8: фундамент remediation-пайплайна (Phase A — decision
preview без executor), большой security-харднинг auth/executor/approval-путей,
серия KG-фиксов качества данных (unfreeze service_health, deploy attribution,
log-error-rate сигнал) и подключение resilience-примитивов в hot-path.

### Security hardening (батчи A–D, 2026-06-09)

- **Authz/integrity в executor/approval-пути** — закрыты дыры авторизации
  на apply/approve; apply/signature-тесты приведены к новому валидатору.
- **Харднинг auth и эндпоинтов** — убраны утечки в ответах, добавлена
  авторизация на ранее открытые ручки, включая `/stats`.
- **Инъекции и ресурсы** — PromQL-инъекция, утечки сессий SQLAlchemy,
  transient-ретраи, alembic-метаданные, webhook insert-race.
- **Стейт-машина пайплайна** — корректность переходов, re-fire неразрешённых
  инцидентов, per-stage timeout; `TRIAGE_REQUIRED` в терминальном skip-сете;
  фикс регрессии retry×stage-timeout.
- **Зависимости**: pyjwt 2.12.0 → 2.13.0 (5 CVE, PYSEC-2026-175..179);
  восстановлены kubernetes/pyjwt pins, снесённые dependabot lock-regen.

### Remediation Phase A — foundation (PR #99)

- Decision preview **без executor**: 8 discrete risk axes, YAML playbooks
  (match/policy/plan/observe), rule-based policy evaluator.
- Ownership: manual manifest для `*-shared` инфраструктуры (PR #96) +
  helm configmap/mount `OWNERSHIP_MANIFEST_PATH` (PR #102) +
  `backfill_ownership` скрипт + periodic beat (PR #92, #94).

### Resilience в hot-path (PR #138)

- Circuit breaker и token-bucket из `resilience.py` подключены к
  LLM-сервису и main: до этого примитивы существовали, но не были wired.

### KG — качество данных и self-health

- **Contract drift guards** (PR #97): `STARTUP_CONTRACT_CHECK` на boot +
  `quality_report --check`; единый источник orphan-метрики, contract 2.3.
- **Разморозки**: alerts_resolve sync freeze + backfill stuck `resolved_at`
  (PR #95); unfreeze `service_health` + canonical `team_owner`.
- **Deploy attribution**: возвращены sha/repo в ns-wide ingest; preprod-деплои
  с веткой `<default>` больше не теряются; canary на ingestion deploy-стрима.
- **Новый сигнал**: per-service log-error-rate из `kg_log_observations` +
  anomaly-consumer; noise floor против z-взрыва на near-flat baseline.
- **Прочее**: race-safe alert upsert + physical-schema PK guard (PR #118),
  jobs linkage fallback-resolver по name pattern (PR #106), metrics_sync
  parallelism + namespace-агрегация (~100% покрытие health, PR #115),
  owner-backfill 61 unowned infra-svc + `expected_stale` для инфра-ns,
  real App-tag extraction `.NET → k8s` mapping в seq-синке.

### Discord / stats digest UX

- Error embed: TL;DR + runbook + severity-визуалы + compact + TC-ссылки
  (PR #110); severity decay >24h + lookup похожих прошлых инцидентов
  (PR #108); inhibit-aware noise reduction + suppress allowlist (PR #109,
  `KubeAPIDown` убран из default suppress в PR #112).
- Stats digest overhaul (PR #111 + фиксы #113–#116): Δ-only, action items,
  MTTR (winsorize + честное окно), deploy correlation, topology growth,
  pipeline gauge, кликабельные TC-билды.

### Squad dashboard (WO-11335, PR #120)

- Авто-генерация Confluence-дашборда занятости сквадов: трёхуровневый
  статус (свободен/занят/протух), живая игровая активность из per-squad
  ClickHouse, честная давность активности.

### Docs

- **`PHILOSOPHY.md`** — зачем проект существует и как его читать.
- LLM-guardrails: каноничный список misleading-сигналов; зафиксировано,
  почему Anthropic prompt caching здесь не применяется.

### Deps / chores

- anthropic 0.40.0 → 0.107.1; sqlalchemy 2.0.50, fastapi 0.136.3,
  uvicorn 0.49.0, structlog 26.1.0.
- CI: conditional skip postgres-зависимых тестов (PR #98), ruff/mypy
  cleanups; `# nosec B104` на намеренный `METRICS_BIND_ADDR=0.0.0.0`.

## [0.12.0] — 2026-05-24 — Wave 8 (KG Metadata + UX Polish)

Метаданные KG и Discord-UX доведены до состояния, в котором можно строить
поверх них Phase A (remediation pipeline). Wave 8 — это 17 PR одной
сессией: четыре расширения KG (Jobs/Storage/Owner-multi-signal/stale_class)
+ формализованный schema/quality contract v2.2 + пять UX-итераций по
Discord embed и daily stats digest + автономный quality_report-скрипт +
snapshot-фикстуры для UX regression-guard + два hotfix-а зависимостей.

### KG Coverage — новые источники и атрибуты

#### Wave 8-A: k8s Jobs/CronJobs coverage (PR #82, `0630c7d`)

- **`app/knowledge_graph/k8s_jobs_sync.py`** (531 LoC, +422 LoC тестов) +
  миграция `alembic/versions/20260524_0000_add_kg_k8s_jobs.py`. Beat task
  `kg_jobs_sync` каждые 15 минут делает `kubectl get jobs,cronjobs -A -o json`
  и upsert'ит в новую таблицу `kg_k8s_jobs`.
- **Closes blind spot**: до Wave 8 KG видел только Deployments/StatefulSets;
  alembic migration jobs, backup CronJob-ы (etcd snapshot, postgres dumps,
  push-s3), ad-hoc cleanup/reindex/cdn-prewarm — оставались невидимыми.
- **Поля Jobs**: `succeeded_count` / `failed_count` / `active_count` /
  `completion_time` / `last_pod_exit_code` (из podStatus последнего pod-а
  по label-selector `job-name=<name>`).
- **Поля CronJobs**: `schedule` / `last_schedule_time` /
  `last_successful_time` / `suspended`.
- **Semantic edge `runs_as_job`**: CronJob → owner Service реализован
  через `K8sJob.owner_service_id` metadata-column (а не отдельный edge-row
  в `kg_service_edges`). Match через label `app.kubernetes.io/part-of` или
  `app` совпадающий с `kg_services.name` в том же namespace.
- CLI: `python -m app.knowledge_graph.k8s_jobs_sync [namespace]`.

#### Wave 8-B: PVC/PV storage coverage (PR #84, `b165dbc`)

- **`app/knowledge_graph/k8s_storage_sync.py`** (660 LoC, +581 LoC тестов)
  + миграция `alembic/versions/20260524_0100_add_kg_storage_volumes.py`.
  Beat task `kg_storage_sync` каждые 30 минут.
- **Closes blind spot**: ClickHouse / Postgres «упал» = в 95% случаев
  диск кончился; до Wave 8 KG не имел никакого storage-слоя.
- **Новые таблицы**: `kg_storage_volumes` (PVC/PV; `kind ∈ {pvc, pv}`;
  PVC namespace-scoped, PV cluster-scoped с `namespace=''`) +
  `kg_volume_edges` (heterogeneous edges с tagged `src_kind`/`dst_kind`).
- **Атрибуты**: storage_class, access_modes, phase (Bound/Pending/Lost/
  Released/Available/Failed), capacity_bytes.
- **Новые edges** (в `kg_volume_edges`):
  - `uses_volume` (Service → PVC) — scan всех pod'ов, для каждого
    `pod.spec.volumes[].persistentVolumeClaim.claimName` → edge от
    owning Service (через ownerReference Deployment/StatefulSet/RS).
  - `bound_to` (PVC → PV) через `pvc.spec.volumeName`.
- **disk_pct enrichment** под флагом `STORAGE_METRICS_ENABLED=false`
  (default OFF — kubelet_volume_stats_* scrape config может быть не
  настроен; без него запрос вернёт 0 для всех PVC и замаскирует реальные NULL).
- CLI: `python -m app.knowledge_graph.k8s_storage_sync`.

#### Wave 8-C: multi-signal owner inference (PR #85, `9c8809c`)

- **`app/services/ownership_suggester.py`** (529 LoC, +507 LoC тестов) +
  **`app/services/owner_aliases.py`** (119 LoC). Расширяет prefix-only
  логику до взвешенного fusion-а трёх независимых сигналов + manual
  override:
  - **A. Prefix** (weight 0.4) — regex по ns-имени (`squad-N-*`,
    `<env>-kingdom<N>`, bare `monitoring`/`kube-system` → `platform`).
  - **B. Deploy history** (weight 0.4) — most-frequent `triggered_by`
    из `kg_deployments` за 30 дней. Username транслируется через
    `owner_aliases.resolve_username` → `@squad-N`/`@platform`.
  - **C. Labels** (weight 0.2) — k8s labels `team` / `owner` / `squad` /
    `app.kubernetes.io/part-of` из `kg_services.metadata_json`.
  - **Manual override** — `OWNERSHIP_MANIFEST_PATH=ownership.yaml` со
    списком `[{ns_pattern, owner, reason}]`. Match по glob → confidence=1.0,
    все эвристики игнорируются.
- `OWNER_ALIASES_PATH=aliases.yaml` для TC-username → team mapping
  (deployment-specific override на pre-baked defaults в коде).
- Backward compat: старая `suggest_owner_for_ns(ns)` оставлена как
  deprecated wrapper.

#### Wave 8-D: stale_class column в kg_services (PR #86, `1f829fd`)

- **`app/knowledge_graph/stale_classifier.py`** (141 LoC, +467 LoC
  тестов) + миграция `alembic/versions/20260524_0200_add_kg_services_stale_class.py`.
- Три значения `kg_services.stale_class`:
  - `active` — deploy за последние `ACTIVE_WINDOW_DAYS` (default 30d).
  - `expected_stale` — давно не катился, но это норма: backup/cron/system
    (`*-backup`, `*-cron`, ns `kube-system`, `monitoring`, …) либо
    infra/platform owner.
  - `suspicious_stale` — нет deploys 30d, не expected_stale.
- Используется `kg_sync.sync_namespace` (переписывает column идемпотентно)
  и `stats_digest.stale_deployments_section` (читает column как primary,
  fallback на legacy `_classify_stale` если column пуст).

#### Wave 8-E: KG schema/quality contract v2.1 → v2.2 (PRs #83 + #89, `75066fa`/`c704244`)

- **`app/knowledge_graph/contract.py`** (506 LoC, +298 LoC тестов в
  `test_kg_contract.py` + `test_contract_drift.py` 247 LoC) +
  **`docs/KG_SCHEMA_CONTRACT.md`** (349 LoC). Формальный реестр
  инвариантов графа: `KG_SCHEMA_VERSION`, `EDGE_KINDS` (semantic /
  src_kinds / dst_kinds / source / status / table), `SERVICE_KINDS` /
  `SYNTHETIC_KINDS`, `OWNER_SOURCES` / `OWNER_SOURCE_ALIASES`,
  `STALE_CLASS_VALUES`.
- Bump v2.1 (Wave 7) → **v2.2** после PR #82/#84/#86: добавлены
  `runs_as_job`, `uses_volume`, `bound_to` edges; promoted `pod_event_of`,
  `serves_traffic`, `routes_to` из `planned` в `active`; новый
  owner source `deploy_history`.
- `STARTUP_CONTRACT_CHECK(db)` — boot-time диагностика: сверяет
  реальные kinds в БД с реестром, логирует drift.
- **Gate #22**: `tests/test_contract_drift.py` auto-validation что
  код и контракт не разъезжаются.

### Discord UX — пять итераций

#### PR #76 — Wave 7 content в enriched embed (`9f82b00`)

- Blast radius (X / Service-Ingress topology), NATS impact (Z / subject
  pub-sub), pod trail (Y / runtime correlation) теперь рендерятся в
  enriched embed как отдельные поля. До этого Wave 7 наполнял KG, но
  embed его не показывал.

#### PR #77 — PATCH-dedup для send_enriched_alert (`da1214e`)

- 30-минутное content-key окно: если тот же логический алерт (по
  hash content) приходит повторно, PATCH-им существующее сообщение
  через `webhook?wait=true`, не репостим. Параллельно к существующему
  fingerprint-dedup для legacy LLM пайплайна.

#### PR #79 — Enriched embed UX polish (`d676057`)

- Human-time («2 hours ago» вместо ISO), pod name, ready/desired
  replicas, kubelet reason — все человекочитаемые сигналы в одном поле.
- Новый модуль `app/utils/time_human.py` (62 LoC).

#### PR #81 — Stats digest UX — 6 пунктов (`d4c339e`)

- `series → series` (один сервис с burst-pattern не разрывает digest на
  отдельные сериес-entries).
- `unowned action block` (видит unowned ns → suggest owner через
  ownership_suggester).
- `trend Δ24h` для всех метрик (vs yesterday-state).
- `alert-types Δ24h`, `chronic`, `resurfaced` — отдельные секции.
- `stale classification` (active/expected/suspicious) с pill «expected:
  скрыто N» при `STATS_HIDE_EXPECTED_STALE=true` (default).
- `fragile → blast-radius` rename — точнее по семантике.

#### PR #90 — Stats digest preview fixes (`bea2d22`)

- Cascade deploys aggregation для multi-squad shared override (one
  push to `prod-shared` → cascades on N kingdom realms; считаем как
  один деплой, не N).
- New-baseline placeholder: `(new baseline)` плейсхолдер вместо
  пустой Δ24h когда yesterday-state не существует.
- Multi-squad shared override для secret-key heuristic.

### Pre-Phase A — quality baseline + snapshot regression-guard

#### PR #87 — quality_report скрипт + baseline snapshot (`bf4dd97`)

- **`app/scripts/quality_report.py`** (785 LoC, +427 LoC тестов).
  Идемпотентный read-only скрипт: 5 групп метрик из Postgres KG
  (services / edges / events / deploys / coverage). Markdown (default)
  или JSON output. Use case — точка отсчёта для Phase A (remediation),
  чтобы demonstrably улучшать metrics, а не угадывать.
- CLI: `python -m app.scripts.quality_report [--json] [--output file]`.
- `docs/quality_report_baseline_2026_05_24.md` (200 LoC) —
  baseline-снимок на момент мерджа Wave 8.

#### PR #88 — Snapshot fixtures gallery (`3b1b8fe`)

- **`tests/fixtures/discord_snapshots/`** — 7 known-good embed cases
  (critical_fresh / critical_resurfaced / warning_compact /
  burst_aggregation / daily_digest / chronic_digest / team_digest) с
  input.json + expected.json. **UX regression-guard**: любое изменение
  в discord/embed_builder валит diff.
- `tests/test_discord_alert_gallery.py` (375 LoC) — раннер.
  Update workflow: `UPDATE_SNAPSHOTS=1 pytest tests/test_discord_alert_gallery.py`.

### Hotfixes / chores

#### PR #74 — PyJWT 2.9 → 2.12 (`9a535c9`)

- CVE-2026-32597 / GHSA-752w-5fwx-jx9f, severity high (Algorithm
  confusion in PyJWT verify when `algorithms` is not explicitly set).

#### PR #75 — gc legacy Discord send_report / send_approval_request (`8a91361`)

- Убраны deprecated `send_report` (celery_worker) и
  `send_approval_request` (service.py) — функциональность мигрировала
  в `send_enriched_alert` ещё в Wave 4, dead code 2 недели.

#### PR #78 / #80 — ruff F841 / F401 fixes (`ff30095` / `9199b6f`)

- Удалены неиспользуемые `svc` локалс и `datetime/timezone` импорты
  в test-файлах Wave 7.

### Schema impact (v2.2)

| Объект | Где живёт | Wave 8 PR |
|---|---|---|
| `kg_k8s_jobs` (table) | новая таблица | #82 |
| `kg_storage_volumes` (table) | новая таблица | #84 |
| `kg_volume_edges` (table) | новая таблица | #84 |
| `kg_services.stale_class` (column) | существующая | #86 |
| Edge `runs_as_job` | `K8sJob.owner_service_id` (metadata-only) | #82 |
| Edge `uses_volume` | `kg_volume_edges` | #84 |
| Edge `bound_to` | `kg_volume_edges` | #84 |

### Beat schedule additions

| Task | Schedule | Module |
|---|---|---|
| `kg_jobs_sync` | every 15 min | `k8s_jobs_sync.py` |
| `kg_storage_sync` | every 30 min | `k8s_storage_sync.py` |

### Documentation

- `docs/KG_SCHEMA_CONTRACT.md` — новый формальный документ контракта.
- `docs/quality_report_baseline_2026_05_24.md` — baseline снимок.
- `docs/ARCHITECTURE.md` / `MODULE_DOCS.md` (EN + RU) обновлены до v2.2 / Wave 8.
- `docs/RUNBOOK.md` / `FAQ.md` (EN + RU) — добавлены секции по
  `quality_report`, ownership manifest, stale_class, snapshot update.

## [0.11.0] — 2026-05-24 — Wave 7 (Topology Expansion)

Расширение источников топологии KG: к env-var heuristic-у (Wave 0) и
Ingress-host externalisation (Phase 1) добавлены три новых declarative и
runtime источника. Цель — снять с env-scan'а монополию на провенанс edges и
дать confidence-фреймворку независимые tier-1 источники для merge.

### Added — Wave 7-Y: PodEvent ↔ ServiceEdge runtime correlation (PR #70, `e9a093c`)

- **`app/knowledge_graph/runtime_correlation.py`** (338 LoC, +215 LoC тестов):
  cheap OTEL-substitute. Beat task `kg_runtime_correlation_sync` каждые 30
  минут ищет пары `(src, dst)` для которых warning-события
  (BackOff/Unhealthy/OOMKilled/FailedScheduling/CrashLoopBackOff/
  FailedMount/ImagePullBackOff) сваливаются в окне `RUNTIME_CORRELATION_WINDOW_MINUTES`
  (default 15 мин) N+ раз за `RUNTIME_CORRELATION_LOOKBACK_DAYS` (default 7d),
  и подтверждает уже существующие edges новым `discovery_source =
  "kg_sync/runtime_corr"` (tier-1 precedence 0.95 в `confidence._SOURCE_PRECEDENCE`).
- **Принципиально не создаёт новые edges из ничего** — симметричный co-fail
  сигнал не определяет direction; полагаемся на declarative-источники для
  топологии, runtime — только confirmation channel.
- **Исключение `_is_synthetic`** — synthetic-узлы (nats-shared, ingress:*)
  игнорируются: их pod_events идут через cluster-wide kubelet и дадут
  false-positive каждому сервису в ns.
- **Feature flag:** `RUNTIME_CORRELATION_ENABLED=true` (включён по умолчанию).
  Конфиг: `RUNTIME_CORRELATION_MIN_COUNT=2`, `RUNTIME_CORRELATION_WINDOW_MINUTES=15`.
- CLI: `python -m app.knowledge_graph.runtime_correlation`.

### Added — Wave 7-X: declarative k8s Service + Ingress topology parser (PR #71, `ba6720e`)

- **`app/knowledge_graph/k8s_topology_resources_sync.py`** (428 LoC, +542 LoC
  тестов): новый declarative источник топологии — golden middle между
  env-var heuristic и runtime-evidence. Beat task
  `kg_topology_resources_sync` каждые 15 минут делает
  `kubectl get services/ingresses -A -o json`, upsert-ит kg_services с
  `metadata_json` (service_type, ports, selector), и строит:
  - **Edge `serves_traffic`** (Service → backing Deployment): declarative
    замена runtime Endpoint-resolve. Selector-match на pod template labels —
    статичный, без зависимости от живых pods.
  - **Edge `routes_to`** (Ingress → backend Service): параллельный slice
    к существующему `k8s_ingress_sync` (`ingress:<host>` → `calls`). Один
    Ingress ресурс может иметь N hosts/paths; новый источник покрывает
    Ingress-as-resource, старый — Ingress-as-host. Оба пишутся в `extras.discovery_sources`
    через `populator.upsert_edge`-merge.
- **ClusterRole RBAC patch** в `k8s/base/rbac.yaml` и
  `helm/sre-ai-copilot/templates/rbac.yaml`: `services`, `ingresses` на verbs
  `get`/`list`/`watch` для cluster-wide read.
- **Включён по умолчанию** — нет feature flag, declarative и idempotent.
- CLI: `python -m app.knowledge_graph.k8s_topology_resources_sync [namespace]`.

### Added — Wave 7-Z: NATS subjects parser из WO monorepo (PR #72, `56155fc`)

- **`app/knowledge_graph/nats_subjects_sync.py`** (639 LoC, +357 LoC тестов
  + C# fixtures): локальный shallow clone WO monorepo + sparse-checkout
  `GR.Platform*` / `GR.WO.*`, regex-парсер C# исходников на consumers
  (`NatsJetStreamConsumer<T>.Subject => NatsSubjectConst.<NAME>`) и
  publish call-sites (`SendToJetStreamAsync(subject: ...)`).
- **Subject как synthetic-Service**: регистрируется в namespace
  `nats-subjects`, `name=subject:<value>` (например `subject:march-export`).
  Переиспользует существующую схему `kg_services`/`kg_service_edges` — без
  новых таблиц и миграций.
- **Edge `uses_nats`** с `extras.direction ∈ {pub, sub}`, `weight = count(call-sites)`.
  Дополняет существующий `uses_nats` к synthetic NATS-cluster-узлам из
  `kg_sync._extract_nats_clusters` (env-vars) — теперь видно не только что
  сервис подключён к кластеру, но и какие subjects он публикует/читает.
- **Service-name резолвинг**: путь `GR.WO.Map.Service/...` → `map-service`
  (lowercase, dots→dash) — матчится с именами Deployment-ов в k8s.
- **NatsSubjectConst-resolver**: один проход по
  `GR.Platform/DataBus/Nats/NatsConst.cs` для `<NAME>` → литерал.
- **Beat task `kg_nats_subjects_sync`** каждые 6 часов, offset minute=43.
- **Feature flag:** `NATS_SUBJECTS_PARSER_ENABLED=false` (выключен по
  умолчанию — требует ssh-доступ к wo-gitlab и каталога `WO_MONOREPO_PATH`,
  включается осознанно после `--dry-run` прогона).
- CLI: `python -m app.knowledge_graph.nats_subjects_sync [--dry-run] [--path PATH]`.

### Fixed — runtime crash dep gap (PR #73, `2a5d9ab`)

- `requirements.txt`: добавлены `PyJWT` и `kubernetes` — обе использовались
  кодом (`app/auth.py`, k8s client-modules), но отсутствовали в lock-е,
  что давало `ModuleNotFoundError` при холодном старте в новом окружении.

### Schema impact

Новые edge `kind`-ы в `kg_service_edges` (`ServiceEdge.kind` — свободная
строка, валидация на app-уровне, миграция не требуется):

| Edge kind | Source module | Direction |
|---|---|---|
| `serves_traffic` | `k8s_topology_resources_sync` | Service → Deployment |
| `routes_to` | `k8s_topology_resources_sync` | Ingress → Service |
| `uses_nats` (subject-level) | `nats_subjects_sync` | Service → `subject:<value>` |

Новый `discovery_source` для existing edges (через merge в `populator.upsert_edge`):

| Source key | Tier | Module |
|---|---|---|
| `kg_sync/runtime_corr` | 1 (0.95) | `runtime_correlation` |
| `k8s_topology_resources/service` | 1 | `k8s_topology_resources_sync` |
| `k8s_topology_resources/ingress` | 1 | `k8s_topology_resources_sync` |
| `nats_subjects_sync` | 1 | `nats_subjects_sync` |

### Beat schedule additions

| Task | Schedule | Module |
|---|---|---|
| `kg_runtime_correlation_sync` | every 30 min | `runtime_correlation.py` |
| `kg_topology_resources_sync` | every 15 min | `k8s_topology_resources_sync.py` |
| `kg_nats_subjects_sync` | every 6h @ :43 | `nats_subjects_sync.py` |

## [0.10.0] — 2026-05-23 — Alert quality batch (Wave 9-style)

Tag-only release. Содержание описано в release notes
([v0.10.0](https://github.com/froggychips/sre-ai-copilot/releases/tag/v0.10.0)):
resolver + rollout suppress + content-dedup + stuck-alerts escalation (PR #69).

## [0.9.0] — 2026-05-23 — Active Observability Layer (Wave 1-6)

Tag-only release. Содержание описано в release notes
([v0.9.0](https://github.com/froggychips/sre-ai-copilot/releases/tag/v0.9.0)):
time-series materialization (kg_service_health/cluster_observations/
ingress_observations/signal_aggregates), anomaly detection (robust-z),
deploy↔incident correlator, Seq logs integration, daily team digest,
Discord pipeline overhaul (dedup/severity/per-team routing/suspect-deploy/
log-error/anomaly/recurrence blocks), security hardening (PII redaction,
Approve/Decline authz allowlist, `kg_reader` RO-роль), self-health canary
(materialization_zero_rate/sync_lag/anomaly_signal_health/...).

## [0.8.0] — 2026-05-16 — KG Phase 1+2+3

Tag-only release. KG content/quality/embed UX:
precedence-model + service health score (PR #51-#53). См.
[v0.8.0 release notes](https://github.com/froggychips/sre-ai-copilot/releases/tag/v0.8.0).

## [0.7.3] — 2026-05-14

### Changed — Light scrub of internal infrastructure references

Pet-проект становится переносимым на любую инфру; defaults в коде не несут
больше имён клиентской инфраструктуры.

- `app/config.py` defaults:
  - `OTLP_EXPORTER_ENDPOINT`: было `"http://jaeger.monitoring:4317"` → `""`
    (пустой → setup_telemetry probe-skip exporter, см. v0.7.2).
  - `GITLAB_BACKEND_PROJECT`: было `"new-wo/backend-services"` → `""`.
  - `JIRA_PROJECT_KEY`: было `"WO"` → `""`.
  - Новый `KG_SCAN_NAMESPACES: str` (comma-separated, default `""`).
- `app/knowledge_graph/kg_sync.py`:
  - Хардкоженный `DEFAULT_SCAN_NAMESPACES` со списком 16 namespace-ов
    выпилен. Источник списка теперь:
    1. Аргумент `namespaces` функции `sync_topology`.
    2. `settings.KG_SCAN_NAMESPACES` (env, comma-separated).
    3. `_discover_namespaces()` — `kubectl get ns` минус
       `kube-*`/`openshift-*`/`cattle-*`/`monitoring`/`default` etc.
- `.env.example` переписан в generic-форму: убраны `lastoasisgame-local`,
  `mcp-teamcity.lastoasisgame.com`, `wo-teamcity.lastoasisgame.com`,
  `Wo_Backend_K8sNewCluster`, `prod-shared`-примеры.
- Тесты: +7 кейсов в `tests/test_kg_enrichment.py` на
  `_discover_namespaces` (system-prefix exclusion, kubectl-failure handling)
  и `sync_topology` priority (explicit arg / settings / fallback discovery).

### Note — history rewrite не делался

Прошлые commit messages и PR-описания (#28-#31) могли содержать
internal-infra строки; они остаются в git-log как есть. Scrub только
для новых коммитов вперёд и текущего tracked состояния.

## [0.7.2] — 2026-05-14

### Added

- **OTEL graceful-degrade на unreachable collector** (`app/telemetry.py`, PR #31):
  при `setup_telemetry` quick TCP-probe (`socket.create_connection`, timeout=2s)
  к OTLP-endpoint. Если недоступен → log.warning + НЕ подключаем
  `BatchSpanProcessor`. Убирает `Transient error... StatusCode.UNAVAILABLE`
  спам в логах api/worker. TracerProvider всё равно set'ится — spans
  создаются в памяти (StageTimer, incident_span работают), просто не
  экспортируются. +11 тестов в `tests/test_telemetry_graceful_degrade.py`.
- **KG topology enrichment** (`app/knowledge_graph/kg_sync.py`, PR #30):
  - `_derive_team_owner(namespace)`: regex по env-prefix
    (`prod-/preprod-/preupdate-`) извлекает team-имя.
  - `_extract_nats_clusters(deploy, namespace)`: парсит env-имена
    с NATS-prefix (`SHARED_NATS_*`, `KINGDOM_NATS_*`, `NATS_FOR_*`)
    и создаёт synthetic-node `nats-{shared|kingdom|purpose}` +
    `edge_kind="uses_nats"`. Synthetic-узлы помечаются
    `team_owner="platform"`.
  - Adds: 16 team_owner-аннотированных WO-namespace-ов → ~95% coverage,
    ~600+ `uses_nats` edges (по сэмплингу real env-vars).
  - +11 тестов в `tests/test_kg_enrichment.py`.

### Fixed

- **Prod hotfix: убран `celery_app.control.shutdown()` из FastAPI lifespan**
  (`app/main.py`, PR #29). Это был broker-wide broadcast через Redis,
  который шатдаунил ВСЕ worker-pods при rolling restart любого api-pod.
  Обнаружено после deploy v0.7.0 → RESTARTS 2/3 у worker-pods. Worker-ы
  реагируют на SIGTERM от k8s сами. +1 regression-тест.

## [0.7.0] — 2026-05-13

### Added — Executor track

The pipeline can now propose, validate, and (opt-in) execute Kubernetes actions
with human approval. Out-of-the-box behaviour is unchanged (advisory-only) —
both flags default to `false`.

- **Structured `ExecutionIntent`** (`app/agents/fix.py`, `app/core/execution_dsl.py`):
  `FixAgent.suggest()` now returns `(raw_text, Optional[ExecutionIntent])`.
  `ExecutionIntent.from_llm_response()` parses LLM output (plain JSON, code-fence
  wrapper, prose-prefix), validates via pydantic with `FORBIDDEN_NAMESPACES`
  rejected at parse time. Persisted in `record.analysis.execution_intent`.
  10 parser tests + 4 contract tests.
- **Executor stage** (`app/workers/pipeline.py::stage_executor`, between `risk`
  and `synthesize`): when `EXECUTOR_ENABLED=true` runs
  `K8sService.execute_intent(intent, dry_run=True)` →
  `kubectl ... --dry-run=server`. `K8sSecurityGuard.validate` fires first;
  on `GUARDRAIL_BLOCK` → `status=guardrail_blocked`; exception → `status=error`
  with advisory-fallback (pipeline doesn't fail). Result persisted in
  `executor_result`. OTEL attribute `sre.incident.executor_status`. 5 tests.
- **Discord Apply button** (`app/services/discord_service.py`,
  `app/api/discord_interactions.py`): when `EXECUTOR_APPROVAL_ENABLED=true` and
  `dry_run_ok` and `risk ∈ {low, medium}`, the embed gets a `⚙️ Apply (kubectl)`
  button. Two-step confirmation (mirror of 👎 pattern). Discord deferred
  response (type=5) handles >3s kubectl operations via PATCH followup webhook.
  12 tests for the handler + deferred path.
- **`app/services/executor_apply.py`**: canonical apply service. Eligibility
  check (intent present, dry-run ok, risk ≤ medium, not already applied) →
  `k8s_service.execute_intent(dry_run=False, post_approval=True)` → persists
  `executor_applied` with timestamp + `applied_by` (Discord user). Idempotency
  by `incident_id`. 8 tests.
- **`K8sService` restructured** (`app/services/k8s_service.py`): guard validates
  via structural `ActionType → (verb, resource)` mapping (`RESTART_DEPLOYMENT
  → (patch, deployments)`, etc.) instead of brittle kubectl-string parsing.
  Fixes pre-existing latent bug where `cmd_parts[2]="restart"` would fail
  `ALLOWED_RESOURCES` check on every `rollout restart`. `post_approval=True`
  required to bypass `SAFE_MODE` for real writes. 8 tests.
- **Settings**: `EXECUTOR_ENABLED`, `EXECUTOR_APPROVAL_ENABLED`,
  `DISCORD_APPLICATION_ID` (for Discord deferred-response followups).
- **Helm chart**: `env.executorEnabled` / `env.executorApprovalEnabled`
  wired into api + worker Deployments.

### Added — Advisory-prod hygiene

- **mypy: 176 → 0 errors, gate is blocking** in CI (`mypy.ini` with per-module
  overrides for SQLAlchemy-ORM `Column[T]` noise + real fixes for `union-attr`
  / `arg-type` in `auth.py`, `discord_service.py`, `llm_service.py`,
  `teamcity_service.py`, `celery_worker.py`, `execution_dsl.py`).
- **ruff: blocking** in CI (`continue-on-error` removed).
- **bandit medium-blocking** preserved; SQL injection f-string in
  `statics_service.py` replaced with `psycopg2.sql.Identifier`; 4 documented
  `# nosec` annotations for false positives.
- **pip-audit blocking** in CI (`protobuf 4.25 → 5.29.6` for CVE-2026-0994,
  `python-dotenv 1.0 → 1.2.2` for CVE-2026-28684; OTEL 1.21 → 1.41.1 to
  satisfy `protobuf<5.0` upper bound).
- **FastAPI lifespan migration**: `@app.on_event` (deprecated since FastAPI
  0.93) → `asynccontextmanager`. Also fixed latent `await engine.dispose()`
  TypeError on a sync `Engine`.
- **`asyncio.run` in Celery task** instead of deprecated `get_event_loop()`.
- **Redis-backed rate limiter** (`app/api/rate_limit.py`): fixed-window 60s
  via Redis INCR+EXPIRE, fail-open on Redis errors. Replaces in-memory
  `defaultdict` that didn't work with multi-replica api.
- **GitHub branch protection** on `master`: required check `Lint and Test`,
  force-push and deletion forbidden, admin can bypass (single-author
  pragmatism).
- **4 pre-existing test failures fixed** (synthesis Russian markers,
  auto_populator fixture, async_integration LLM_BACKEND patch).

### Added — Documentation

- `README.md` (EN + RU): rewritten "Advisory mode" → "Auto-remediator with
  advisory-fallback (off by default)" with ramp-up plan. Roadmap → Execution
  reflects 3/4 done.
- `docs/RUNBOOK.md` (EN + RU): new "Executor incidents" section — how to
  recognize an executor incident, recover from a failed apply, kill the
  executor, audit-trail event types.
- `CHANGELOG.md`: this entry.

## [0.5.0] — 2026-05-12

### Added
- **Fact-anchored reasoning** (`app/diagnostics/`): deterministic rule engine runs before any LLM agent and produces a typed `FactStore` with `FactKind` slugs, confidence scores, and supporting evidence.
- **Multi-hypothesis pipeline** (`app/agents/multi_hypothesis.py`): parallel fan-out to four perspectives (app/infra/deps/runtime); results are adversarially grounded by `FactCriticAgent`.
- **PERSPECTIVE_PRECONDITIONS**: runtime perspective only activates when `process_crash` is observed — prevents noise from unfounded LLM speculation.
- **Fact conflict detection** (`MUTUALLY_EXCLUSIVE_PAIRS`): `{oom_killed, process_crash}` is a contradiction; both observed=True triggers confidence cap at 0.60, `evidence.conflict_with` annotation, and a `<conflicts>` block in the prompt context.
- **OOMKilledRule structured gate** (`app/diagnostics/rules/oom.py`): `_check_pod_state()` scans all pods in `k8s_pod_state`, target pod first; if target exit code ≠ 0 and ≠ 137, returns `observed=False` and skips text-regex fallback — eliminates false positives from other pods' events.
- **KG quality gate** (`_is_quality_cause()`): only high-quality causes are written to the Knowledge Graph; filters out `None`, "No hypothesis survived…", "Manual triage required" strings.
- **Recurrence detection** (`app/core/intelligence/similar_incidents.py`): `RECURRENCE_WINDOW_DAYS=7`; past incidents for the same service that were resolved within the window set `recurrence=True`.
- **FixAgent recurrence mode** (`_RECURRENCE_PREFIX`): when `is_recurrence=True`, FixAgent is instructed to recommend investigative actions (get_logs, describe_resource), not another restart.
- **Jira enrichment** (`app/context/jira_client.py`): `JiraClient` queries Atlassian REST API (Basic Auth); `build_jira_context()` separates open/resolved issues; `_build_jira_prefix()` prepends known issues to FixAgent context.
- **Jira config keys** in `app/config.py`: `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY`, `JIRA_BACKEND_LABEL`, `JIRA_SEARCH_DAYS`.
- **analysis fields**: `cause` (None when no survivor), `triage_note`, `resolution_quality`, `fact_conflicts`, `is_recurrence`, `jira_context`.
- **Helm chart** (`helm/sre-ai-copilot/`): full Helm chart with API deployment, Celery worker, Redis StatefulSet, NetworkPolicy, RBAC, PDB, Ingress (nginx + cert-manager + oauth2-proxy).
- **Bilingual documentation** (EN + RU): `docs/RUNBOOK.md`, `docs/RUNBOOK.ru.md`, `docs/ARCHITECTURE.ru.md`, `docs/MODULE_DOCS.ru.md`.

### Changed
- `app/workers/tasks.py`: stage ordering now includes Jira enrichment (stage 4) before FixAgent (stage 5); `is_recurrence` flag wired from SimilarIncidentEngine to FixAgent.
- `app/agents/fix.py`: `suggest()` accepts `is_recurrence` and `jira_context` parameters.
- `docs/ARCHITECTURE.md`: updated to reflect 8-stage pipeline, fact-anchored reasoning, recurrence detection, and Jira integration.
- `docs/MODULE_DOCS.md`: added DiagnosticsEngine, FactStore, JiraClient, SimilarIncidentEngine sections.

### Fixed
- `OOMKilledRule` false positive: text-regex no longer matches OOMKilled events from pods other than the target.
- Knowledge Graph pollution: "Manual triage required" strings no longer stored as resolved causes.
- `SimilarIncidentEngine` data format: `_extract_service_ns()` handles both `targets[0].service` (old) and `labels.service` (new) formats.

### Tests
- `tests/test_diagnostics.py`: 6 conflict detection tests, 4 OOMKilledRule structured gate tests.
- `tests/test_knowledge_graph.py`: 5 KG quality gate tests, 5 recurrence detection tests.
- `tests/test_jira_client.py`: 10 tests covering `_jira_status`, `build_jira_context`, `JiraClient.search_by_service` (mocked httpx).
- `tests/test_multi_hypothesis.py`: 3 perspective precondition tests, updated fan-out tests.

---

## [0.4.0] — 2026-05-10

### Added
- TeamCity MCP integration (`app/services/teamcity_service.py`): enriches incident context with recent deploy data.
- VictoriaMetrics context window (`VICTORIA_METRICS_WINDOW_MINUTES`): memory/CPU metrics before the incident.
- Prompt injection guard (`app/services/prompt_guard.py`): detects injection attempts in external data before it reaches agent prompts.

### Changed
- Context builder now queries TeamCity and VictoriaMetrics in parallel before the agent pipeline.

---

## [0.3.0] — 2026-05-01

### Added
- Human approval flow: `POST /approvals/{incident_id}/approve` and `/reject`.
- Discord webhook integration with dry-run mode (`DISCORD_DRY_RUN`).
- Replay endpoint (`POST /replay/{incident_id}`): re-runs analysis without side effects.
- JWT auth dependency for `/copilot` endpoints.

---

## [0.2.0] — 2026-04-15

### Added
- Celery + Redis async task processing.
- `ExecutionIntent` DSL with `K8sSecurityGuard` policy validation.
- Feedback and evaluation endpoints (`/evaluation`).

---

## [0.1.0] — 2026-04-01

### Added
- Initial release: FastAPI webhook receiver, single-perspective LLM pipeline (Analyzer → Hypothesis → Critic → Fix → Risk), PostgreSQL persistence.
