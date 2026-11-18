"""Ретеншен наблюдательных таблиц — история сэмплов не должна расти вечно.

Замер 15.08.2026: 18.2 млн строк, таблица 4.2 ГБ из 5.3 ГБ всей базы (79%).
Данные копятся с 22.05.2026, темп — около 330 тысяч точек в сутки. Политики
хранения не было вообще: таблица просто росла с первого дня.

**Сколько нужно на самом деле.** Глубина, на которую эти данные реально
читают, оказалась много меньше накопленного:

    anomaly_detection.BASELINE_DAYS            7 дней
    health_score._METRIC_BASELINE_WINDOW_DAYS  7 дней
    deploy_correlator.lookback_hours           2 часа

То есть при 85 днях истории самый глубокий потребитель смотрит на неделю.
Дефолт 30 дней даёт четырёхкратный запас к этому окну и всё равно убирает
больше половины таблицы.

**Почему не по доле.** Соблазн «удалять, пока таблица не похудеет на N%»
здесь такой же ложный, как guard `drift_pct > 20%` в namespace-уборке: доля
привязывает решение к текущему состоянию, а не к смыслу. Срок хранения —
свойство данных («метрики недельной давности не нужны никому»), и считать его
надо по времени.

**Защита от собственной ошибки.** Единственный способ навредить этим модулем —
ошибиться в cutoff и снести свежее. Поэтому `MIN_RETENTION_DAYS` — жёсткий пол:
запрос удалить историю за меньший срок не выполняется, а возвращает отказ.
Оставить лишние данные не страшно; удалить нужные — необратимо, бэкапов
per-table у этой БД нет.

Удаление идёт батчами: единственный `DELETE` на 9.8 млн строк держал бы
ACCESS EXCLUSIVE-подобную нагрузку на автовакуум и раздул бы WAL. Каждый батч
коммитится отдельно, прогон ограничен `max_batches` — задача обязана
укладываться в свой тик, а не догонять всё за раз.

**Почему теперь не одна таблица.** Политика была написана для
`kg_service_health` и на ней же и осталась, хотя рядом растут ещё пять
таблиц ровно той же природы — периодические сэмплы, которые читают на
глубину часов. Замер 05.09.2026, строк старше 30 дней:

    kg_anomaly_observations    908 148  (68% таблицы, 638 МБ)
    kg_signal_aggregates       412 167  (50%,          190 МБ)
    kg_ingress_observations    350 196  (73%,          212 МБ)
    kg_log_observations        106 141  (67%,           62 МБ)
    kg_cluster_observations     21 724  (73%,           15 МБ)

При том что самые глубокие потребители смотрят на 1 час
(`VOLUME_GUARD_WINDOW`), 24 часа (self-health, team-digest) и 7 дней
(`BASELINE_DAYS`). `kg_cluster_observations` живёт дольше остальных: она
копеечная по объёму, а поквартальный тренд по кластеру — единственное, из
чего его вообще можно восстановить.

**События — отдельный реестр.** `kg_deployments`, `kg_alerts`,
`kg_pod_events`, `kg_incidents`, `kg_remediation_events` — это события со
смыслом, а не сэмплы: деплой полугодовой давности отвечает на вопрос «когда
это сломалось». Поэтому у них свой список `EVENT_RETENTION_TARGETS` со
сроками от полугода и свой пол `MIN_EVENT_RETENTION_DAYS`, а не 30 дней
метрик. Срок у них всё же есть: до 31.10.2026 его не было вовсе, и
`kg_deployments` дорос до 1,24 млн строк (1,96 ГБ) — больше, чем все
наблюдательные таблицы вместе. У алертов и инцидентов время — `resolved_at`:
открытые (NULL) не трогаются никогда. `kg_k8s_jobs`, `kg_services`,
`kg_service_edges` — не история, а текущее состояние, их чистят синки.

**Сырьё health — неделя, дальше агрегаты** (18.11.2026). 30 дней сырья
`kg_service_health` — это 2,2 ГБ из 3,8 ГБ базы при читателях глубиной в
неделю. Свёртку старше семи суток в часовые агрегаты делает соседний модуль
`health_downsample` (свой beat, своя настройка, по умолчанию выключен);
30-дневный срок здесь остаётся страховкой на случай, когда свёртка
выключена или отстала. Срок жизни узлов снесённых стендов — `gone_retention`.

**Процесс remediation — третий реестр** (18.11.2026).
`kg_remediation_decisions`, `kg_action_approvals`, `kg_remediation_attempts`
и мостовые строки `incidents` (`rb-…`, статус навсегда APPROVAL_PENDING)
срока не имели вовсе. Сегодня там десятки строк, но это ровно то состояние,
в котором `kg_deployments` была до своих 1,96 ГБ. Список —
`WORKFLOW_RETENTION_TARGETS`, включается настройкой
`KG_WORKFLOW_RETENTION_MODE` (off → dry_run → apply).

**Ссылки между таблицами — условием, а не порядком.** Порядок списка
(«сначала события, потом инциденты») закрывал FK
kg_remediation_events → kg_incidents только пока событие создано раньше,
чем закрыт его инцидент. Замер 18.11.2026: у 26 из 487 привязанных событий
`created_at` позже `resolved_at` инцидента (до шести минут — медик
дописывает прогон после закрытия). В такой паре инцидент пересекает границу
первым, DELETE батча падает на FK и откатывается целиком — вместе со всеми
остальными инцидентами батча. Поэтому строка, на которую ещё ссылаются, в
выборку не попадает (`_EXTRA_WHERE`): она уйдёт следующим прогоном, когда
уйдёт ссылка.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, cast

from prometheus_client import Counter
from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

__all__ = [
    "purge_old_health", "purge_observations", "purge_events", "purge_workflow",
    "RETENTION_TARGETS", "EVENT_RETENTION_TARGETS", "WORKFLOW_RETENTION_TARGETS",
    "DEFAULT_RETENTION_DAYS", "MIN_RETENTION_DAYS", "MIN_EVENT_RETENTION_DAYS",
    "DEFAULT_WORKFLOW_RETENTION_DAYS",
    "MODE_OFF", "MODE_DRY_RUN", "MODE_APPLY", "retention_mode", "RETENTION_ROWS",
]

# ── Режимы уборки, удаляющей данные ───────────────────────────────────────
MODE_OFF = "off"
MODE_DRY_RUN = "dry_run"
MODE_APPLY = "apply"


def retention_mode(raw: Any) -> str:
    """Привести значение настройки `*_MODE` к одному из трёх режимов.

    Всё, что не `dry_run` и не `apply` (опечатка, пустая строка, None), —
    `off`: непонятная настройка не должна ничего удалять.
    """
    value = str(raw or "").strip().lower().replace("-", "_")
    if value in (MODE_DRY_RUN, MODE_APPLY):
        return value
    if value not in ("", MODE_OFF):
        logger.warning("retention.unknown_mode value=%r — читаю как off", raw)
    return MODE_OFF


#: Строки, которые уборка удалила (`deleted`), свернула (`folded`,
#: `hourly_upserted`) или удалила бы в режиме dry_run (`would_*`). Счётчик, а
#: не gauge: воркер prefork, значения складывает multiprocess-режим
#: prometheus_client. Суточный объём — `increase(...[1d])`.
RETENTION_ROWS = Counter(
    "kg_retention_rows_total",
    "Rows deleted / folded by KG retention jobs, or that would be in dry-run",
    ["target", "action"],
)

#: Сколько истории храним. 30 дней = 4× самое глубокое окно потребителя (7д).
DEFAULT_RETENTION_DAYS = 30

#: Пол ретеншена. Ниже него модуль отказывается работать: 7 дней — это ровно
#: baseline детектора аномалий, и удаление внутри этого окна сломало бы
#: сравнение «сейчас против нормы», причём молча.
MIN_RETENTION_DAYS = 7

#: Строк за батч. 50k — компромисс: реже коммитов, чем при 10k, и заметно
#: короче транзакция, чем при 500k.
DEFAULT_BATCH_SIZE = 50_000

#: Батчей за прогон. 40 × 50k = до 2 млн строк за тик: первый прогон разберёт
#: накопленное за несколько суток, дальше уборка станет копеечной.
DEFAULT_MAX_BATCHES = 40

#: Пол ретеншена СОБЫТИЙ. Квартал — минимум, на который таймлайн инцидента и
#: вопрос «когда это сломалось» должны отвечать без оговорок; короче —
#: почти наверняка ошибка в настройке.
MIN_EVENT_RETENTION_DAYS = 90


#: Наблюдательные таблицы: (таблица, колонка времени, срок хранения).
#: Срок — свойство данных, а не текущего размера таблицы: см. рассуждение
#: про доли в докстринге модуля.
RETENTION_TARGETS: tuple[tuple[str, str, int], ...] = (
    # Самый глубокий потребитель — baseline детектора аномалий (7 дней).
    ("kg_service_health", "ts", DEFAULT_RETENTION_DAYS),
    # Читают на 1 час (VOLUME_GUARD_WINDOW) и 24 часа (self-health).
    ("kg_anomaly_observations", "ts", DEFAULT_RETENTION_DAYS),
    # Читают на _SIGNAL_AGG_FRESHNESS_HOURS и окно team-digest (24 часа).
    ("kg_signal_aggregates", "window_end", DEFAULT_RETENTION_DAYS),
    # Читают на минуты (_INGRESS_RECENT_WINDOW_MINUTES).
    ("kg_ingress_observations", "ts", DEFAULT_RETENTION_DAYS),
    # Окно вокруг инцидента (часы) плюс BASELINE_DAYS для log_error_rate.
    ("kg_log_observations", "ts", DEFAULT_RETENTION_DAYS),
    # 15 МБ на три месяца: дешевле хранить, чем потом не иметь тренда.
    ("kg_cluster_observations", "ts", 90),
)

#: Таблицы событий: (таблица, колонка времени, срок хранения). Сроки длиннее
#: метрик на порядок — см. «События — отдельный реестр» в докстринге модуля.
EVENT_RETENTION_TARGETS: tuple[tuple[str, str, int], ...] = (
    # «Когда это сломалось» — полгода. Самый глубокий читатель: stale-класс
    # (ACTIVE_WINDOW_DAYS) и deploy-correlator (часы).
    ("kg_deployments", "started_at", 180),
    # По resolved_at: открытый алерт (NULL) не удаляется никогда.
    ("kg_alerts", "resolved_at", 180),
    # По last_seen: событие, которое k8s ещё видит, остаётся.
    ("kg_pod_events", "last_seen", 90),
    # Починки медика — год: это датасет live-RCA и история стендов.
    ("kg_remediation_events", "created_at", 365),
    # По resolved_at — открытые остаются. Инцидент, на который ещё ссылается
    # событие медика (FK), пропускается — см. `_EXTRA_WHERE`.
    ("kg_incidents", "resolved_at", 365),
)

#: Срок хранения таблиц процесса remediation. Полгода: одобрение и попытка —
#: аудит «кто и что разрешил», и разбор полётов по ним идёт месяцы спустя.
DEFAULT_WORKFLOW_RETENTION_DAYS = 180

#: Таблицы процесса remediation: (таблица, колонка времени, срок хранения).
#: Срок приходит из `KG_WORKFLOW_RETENTION_DAYS`, здесь — дефолт. Потомки
#: раньше родителя: FK между ними нет (связь — строковый incident_id), но
#: строка `incidents` уходит только когда на неё никто не ссылается.
WORKFLOW_RETENTION_TARGETS: tuple[tuple[str, str, int], ...] = (
    ("kg_remediation_decisions", "created_at", DEFAULT_WORKFLOW_RETENTION_DAYS),
    ("kg_action_approvals", "created_at", DEFAULT_WORKFLOW_RETENTION_DAYS),
    # По updated_at: попытку трогает отложенная верификация (+5/+15 минут).
    ("kg_remediation_attempts", "updated_at", DEFAULT_WORKFLOW_RETENTION_DAYS),
    ("incidents", "created_at", DEFAULT_WORKFLOW_RETENTION_DAYS),
)

#: Дополнительное условие отбора на таблицу — литералы, не ввод. Оно всегда
#: СУЖАЕТ выборку: строка, которая под него не попала, остаётся.
_EXTRA_WHERE: Dict[str, str] = {
    # FK kg_remediation_events.incident_id → kg_incidents.id (NO ACTION):
    # инцидент со ссылкой ждёт, пока уйдёт событие.
    "kg_incidents": (
        "NOT EXISTS (SELECT 1 FROM kg_remediation_events e"
        " WHERE e.incident_id = kg_incidents.id)"
    ),
    # Только мостовые строки (`rb-…` заводит remediation.bridge): остальное в
    # `incidents` — разборы LLM-пайплайна, у них срока нет, и назначать его
    # заодно незачем. Строка с живым claim-ом исполнителя и строка, на которую
    # ссылаются решение / одобрение / попытка, остаются.
    "incidents": (
        "incident_id LIKE 'rb-%'"
        " AND (executor_state IS NULL OR executor_state <> 'in_flight')"
        " AND NOT EXISTS (SELECT 1 FROM kg_remediation_decisions d"
        " WHERE d.incident_id = incidents.incident_id)"
        " AND NOT EXISTS (SELECT 1 FROM kg_action_approvals a"
        " WHERE a.incident_id = incidents.incident_id)"
        " AND NOT EXISTS (SELECT 1 FROM kg_remediation_attempts t"
        " WHERE t.incident_id = incidents.incident_id)"
    ),
}


def purge_observations(
    db: Session,
    *,
    overrides: Dict[str, int] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int = DEFAULT_MAX_BATCHES,
    now: datetime | None = None,
) -> Dict[str, Any]:
    """Прогнать ретеншен по всем таблицам из `RETENTION_TARGETS`.

    `overrides` — сроки из настроек по имени таблицы (сейчас так приходит
    `KG_HEALTH_RETENTION_DAYS`). Пол `MIN_RETENTION_DAYS` действует и на них.

    Таблицы обрабатываются по очереди и независимо: отказ или ошибка на
    одной не отменяет уборку остальных — иначе одна кривая настройка
    остановила бы политику целиком, а именно так эти таблицы и росли.
    """
    per_table: Dict[str, Any] = {}
    deleted = 0
    for table, ts_column, days in RETENTION_TARGETS:
        try:
            stats = purge_table(
                db, table=table, ts_column=ts_column,
                retention_days=(overrides or {}).get(table, days),
                batch_size=batch_size, max_batches=max_batches, now=now,
            )
        except Exception as e:  # noqa: BLE001 — соседние таблицы не виноваты
            logger.error("retention.table_failed table=%s: %s", table, e)
            db.rollback()
            per_table[table] = {"error": str(e)}
            continue
        per_table[table] = stats
        deleted += stats["deleted"]
    logger.info("retention.done deleted=%s tables=%s", deleted, len(per_table))
    return {"deleted": deleted, "per_table": per_table}


def purge_events(
    db: Session,
    *,
    overrides: Dict[str, int] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int = DEFAULT_MAX_BATCHES,
    now: datetime | None = None,
) -> Dict[str, Any]:
    """Прогнать ретеншен по таблицам событий из `EVENT_RETENTION_TARGETS`.

    Тот же контракт, что у `purge_observations`: таблицы независимы, пол —
    `MIN_EVENT_RETENTION_DAYS`. FK kg_remediation_events → kg_incidents
    закрыт условием отбора (`_EXTRA_WHERE`), а не порядком списка.
    """
    per_table: Dict[str, Any] = {}
    deleted = 0
    for table, ts_column, days in EVENT_RETENTION_TARGETS:
        try:
            stats = purge_table(
                db, table=table, ts_column=ts_column,
                retention_days=(overrides or {}).get(table, days),
                batch_size=batch_size, max_batches=max_batches, now=now,
                min_days=MIN_EVENT_RETENTION_DAYS,
            )
        except Exception as e:  # noqa: BLE001 — соседние таблицы не виноваты
            logger.error("retention.events.table_failed table=%s: %s", table, e)
            db.rollback()
            per_table[table] = {"error": str(e)}
            continue
        per_table[table] = stats
        deleted += stats["deleted"]
    logger.info("retention.events.done deleted=%s tables=%s", deleted, len(per_table))
    return {"deleted": deleted, "per_table": per_table}


def purge_workflow(
    db: Session,
    *,
    retention_days: int = DEFAULT_WORKFLOW_RETENTION_DAYS,
    dry_run: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int = DEFAULT_MAX_BATCHES,
    now: datetime | None = None,
) -> Dict[str, Any]:
    """Прогнать ретеншен по таблицам процесса remediation.

    `dry_run=True` (дефолт) ничего не удаляет: считает строки за границей и
    возвращает их в `would_delete`. Один срок на все четыре таблицы — иначе
    одобрение могло бы пережить попытку, которую оно разрешило. Пол —
    `MIN_EVENT_RETENTION_DAYS`.
    """
    per_table: Dict[str, Any] = {}
    deleted = 0
    would_delete = 0
    for table, ts_column, _days in WORKFLOW_RETENTION_TARGETS:
        try:
            stats = purge_table(
                db, table=table, ts_column=ts_column,
                retention_days=retention_days,
                batch_size=batch_size, max_batches=max_batches, now=now,
                min_days=MIN_EVENT_RETENTION_DAYS, dry_run=dry_run,
            )
        except Exception as e:  # noqa: BLE001 — соседние таблицы не виноваты
            logger.error("retention.workflow.table_failed table=%s: %s", table, e)
            db.rollback()
            per_table[table] = {"error": str(e)}
            continue
        per_table[table] = stats
        deleted += stats["deleted"]
        would_delete += stats.get("would_delete", 0)
    logger.info(
        "retention.workflow.done dry_run=%s deleted=%s would_delete=%s",
        dry_run, deleted, would_delete,
    )
    return {
        "dry_run": dry_run, "deleted": deleted, "would_delete": would_delete,
        "per_table": per_table,
    }


def purge_old_health(
    db: Session,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int = DEFAULT_MAX_BATCHES,
    now: datetime | None = None,
) -> Dict[str, Any]:
    """Удалить точки `kg_service_health` старше `retention_days`.

    Частный случай `purge_table` — оставлен как есть ради вызывающих,
    которые знают только про метрики.
    """
    return purge_table(
        db, table="kg_service_health", ts_column="ts",
        retention_days=retention_days, batch_size=batch_size,
        max_batches=max_batches, now=now,
    )


#: Имена таблиц и колонок подставляются в SQL текстом (параметризовать
#: идентификаторы нельзя), поэтому берутся только из `RETENTION_TARGETS` и
#: сверяются с ним же: снаружи произвольное имя таблицы сюда не попадёт.
_ALLOWED_TABLES = {
    t: c for t, c, _ in (
        *RETENTION_TARGETS, *EVENT_RETENTION_TARGETS, *WORKFLOW_RETENTION_TARGETS,
    )
}


def purge_table(
    db: Session,
    *,
    table: str,
    ts_column: str,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int = DEFAULT_MAX_BATCHES,
    now: datetime | None = None,
    min_days: int = MIN_RETENTION_DAYS,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Удалить строки `table` старше `retention_days` по колонке `ts_column`.

    `min_days` — пол: `MIN_RETENTION_DAYS` для сэмплов,
    `MIN_EVENT_RETENTION_DAYS` для событий (`purge_events`).

    `dry_run=True` — только посчитать (`would_delete` в ответе и метрика
    `kg_retention_rows_total{action="would_delete"}`), ничего не удаляя.

    Возвращает статистику прогона:
      * `deleted` — сколько строк удалено;
      * `batches` — сколько батчей выполнено;
      * `truncated` — упёрлись в `max_batches` (осталось на следующий тик);
      * `cutoff` — граница, старше которой удаляли;
      * `skipped` — причина отказа, если ничего не делали.

    Отказ (ничего не удаляя) при `retention_days < MIN_RETENTION_DAYS`:
    такой запрос почти наверняка ошибка, а последствия необратимы.
    """
    if _ALLOWED_TABLES.get(table) != ts_column:
        raise ValueError(
            f"{table}.{ts_column} нет в RETENTION_TARGETS: срок хранения "
            "таблицы — решение, которое принимают явно, а не аргументом "
            "вызова"
        )
    if retention_days < min_days:
        logger.error(
            "retention.refused table=%s retention_days=%s < min=%s",
            table, retention_days, min_days,
        )
        return {
            "deleted": 0, "batches": 0, "truncated": False, "cutoff": None,
            "skipped": (
                f"retention_days={retention_days} меньше минимума "
                f"{min_days}: внутри этого окна лежат данные, которые ещё "
                f"читают (baseline детектора аномалий / таймлайн инцидентов)"
            ),
        }

    cutoff = (now or datetime.utcnow()) - timedelta(days=retention_days)
    extra = _EXTRA_WHERE.get(table)
    # Имя таблицы и колонки сверены с реестрами выше, `extra` — литерал из
    # `_EXTRA_WHERE` этого модуля; значения идут bind-параметрами.
    where = f"{ts_column} < :cutoff" + (f" AND {extra}" if extra else "")
    deleted = 0
    batches = 0

    if dry_run:
        would = int(db.execute(
            text(f"SELECT count(*) FROM {table} WHERE {where}"),  # nosec B608
            {"cutoff": cutoff},
        ).scalar() or 0)
        db.rollback()   # чтение не должно оставлять открытую транзакцию
        RETENTION_ROWS.labels(target=table, action="would_delete").inc(would)
        dry_stats: Dict[str, Any] = {
            "table": table, "deleted": 0, "would_delete": would, "batches": 0,
            "truncated": False, "cutoff": cutoff.isoformat(), "skipped": None,
            "dry_run": True,
        }
        logger.info("retention.table_dry_run %s", dry_stats)
        return dry_stats

    for _ in range(max_batches):
        # DELETE ... WHERE id IN (SELECT ... LIMIT n) — портируемый способ
        # ограничить батч: у PostgreSQL нет LIMIT в DELETE, а ORDER BY ts
        # даёт предсказуемый порядок (сначала самое старое).
        # cast: у DML-запроса execute() возвращает CursorResult с rowcount,
        # но стабы объявляют общий Result — по нему счётчика не видно.
        result = cast(
            CursorResult,
            db.execute(
                # nosec B608 — имя таблицы и колонки не приходят снаружи: оба
                # сверены с RETENTION_TARGETS выше (ValueError на чужое), а
                # параметризовать идентификаторы в SQL нельзя. Значения
                # (cutoff, lim) идут bind-параметрами.
                text(
                    f"DELETE FROM {table} WHERE id IN ("  # nosec B608
                    f"  SELECT id FROM {table}"
                    f"  WHERE {where}"
                    f"  ORDER BY {ts_column} LIMIT :lim"
                    ")"
                ),
                {"cutoff": cutoff, "lim": batch_size},
            ),
        )
        # Коммит на каждый батч: транзакция живёт ровно один батч, и
        # прерывание прогона не откатывает уже сделанную работу.
        db.commit()

        rows = result.rowcount or 0
        deleted += rows
        batches += 1
        if rows < batch_size:
            break   # старых строк больше не осталось

    RETENTION_ROWS.labels(target=table, action="deleted").inc(deleted)
    truncated = batches >= max_batches and deleted >= max_batches * batch_size
    stats: Dict[str, Any] = {
        "table": table,
        "deleted": deleted,
        "batches": batches,
        "truncated": truncated,
        "cutoff": cutoff.isoformat(),
        "skipped": None,
    }
    if truncated:
        # Молча оставленный хвост выглядел бы как «убрано всё».
        logger.warning(
            "retention.truncated table=%s deleted=%s — упёрлись в "
            "max_batches=%s, остаток уйдёт следующим тиком",
            table, deleted, max_batches,
        )
    logger.info("retention.table_done %s", stats)
    return stats
