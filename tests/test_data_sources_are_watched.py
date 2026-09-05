"""У каждого источника данных должен быть надзор за фактом прогона.

Ревизия 23.08.2026 нашла системную слепую зону: восемь задач, пополняющих
граф, были в расписании beat, но ни в `_SYNC_LAG_TARGETS`, ни в
`_BEAT_HEARTBEAT_TASKS`. Смерть любой из них не замечал никто.

Две проверки её вдобавок маскировали:

  * `pod_events_link_rate` при нуле событий возвращает ok — «нечего
    связывать», и это правда;
  * `alerts_resolve_freshness` считает открытые алерты старше недели, и от
    остановки притока их число не растёт.

Каждая честно отвечала на свой вопрос. Вопроса «а источник вообще жив» не
задавал никто.

Прецедент того же класса известен: `kg_seq_logs_sync` 20.08.2026 — синк
ходил, NetworkPolicy рубила запросы, отчёт был «rows=0», и 12,8 часа это
выглядело нормально.

Свежесть источников проверяется по heartbeat, а не по данным, и это
принципиально: пустое окно бывает законным (в кластере тихо), отсутствие
прогона — никогда.
"""
import pytest

from app.knowledge_graph.self_health import _SYNC_LAG_TARGETS
from app.workers.tasks import _BEAT_HEARTBEAT_TASKS, celery_app

#: Задачи, которые ПОПОЛНЯЮТ граф. Их молчание делает данные неверными, а не
#: просто неполными, поэтому за фактом прогона нужен надзор.
_DATA_SOURCES = frozenset({
    "k8s_pod_events_sync",
    # Выкаты, замеченные в самом кластере. Второй источник kg_deployments —
    # и потому особенно незаметный при остановке: записи из TeamCity
    # продолжают идти, и таблица выглядит живой.
    "kg_deploy_watch",
    "kg_alerts_resolve_sync",
    "kg_cluster_health_sync",
    "kg_endpoints_sync",
    "kg_ingress_observations_sync",
    "kg_ingress_sync",
    "kg_jobs_sync",
    "kg_metrics_sync",
    "kg_nats_subjects_sync",
    "kg_runtime_correlation_sync",
    "kg_seq_logs_sync",
    "kg_statics_versions_sync",
    "kg_storage_sync",
    "kg_topology_resources_sync",
    "kg_topology_sync",
})

#: Задачи обслуживания: ремонт, чистка, отчёты. Их молчание данные не
#: искажает — граф просто не приберётся, и это видно другими способами.
#: Перечислены явно, чтобы новая задача не проскочила мимо ревизии.
_MAINTENANCE = frozenset({
    "kg_db_edge_rehome",
    "kg_drift_cleanup",
    "kg_health_recompute",
    "kg_health_retention",
    "kg_namespace_lifecycle",
    "kg_ownership_backfill",
    "kg_self_health_check",
    "kg_signal_aggregates_compute",
    "kg_stuck_alerts_check",
    "kg_anomaly_detection_task",
    # tc_deploys_to_kg — источник, но за ним следит отдельная семантическая
    # проверка deploy_stream_ingestion: она сверяет, что отданное TC доехало
    # до графа, то есть отвечает на более сильный вопрос, чем heartbeat.
    "tc_deploys_to_kg",
    "chronic_alerts_digest",
    "daily_stats_digest",
    "team_daily_digest",
    "discord_dedup_purge",
    "kg_external_probe",
})


def _scheduled_tasks():
    return {cfg["task"] for cfg in celery_app.conf.beat_schedule.values()}


@pytest.mark.parametrize("task", sorted(_DATA_SOURCES))
def test_every_data_source_has_a_heartbeat(task):
    """Без heartbeat смерть синка видна только по отсутствию данных.

    А отсутствие данных бывает законным — именно поэтому нужен отдельный
    сигнал о самом прогоне.
    """
    assert task in _BEAT_HEARTBEAT_TASKS, (
        f"{task} пополняет граф, но не пишет heartbeat: его остановку "
        "заметит только тот, кто догадается сверить данные вручную"
    )


@pytest.mark.parametrize("task", sorted(_DATA_SOURCES))
def test_every_data_source_freshness_is_checked(task):
    """Heartbeat бесполезен, если его никто не читает."""
    assert task in _SYNC_LAG_TARGETS, (
        f"{task} пишет heartbeat, но sync_lag его не проверяет — "
        "сигнал есть, потребителя нет"
    )


def test_no_scheduled_task_escapes_classification():
    """Новая beat-задача обязана попасть в один из двух списков.

    Иначе повторится ровно то, что нашла ревизия 23.08.2026: задача в
    расписании, надзора нет, и никто этого не замечает, пока не сломается.
    """
    unclassified = sorted(
        _scheduled_tasks() - _DATA_SOURCES - _MAINTENANCE
    )
    assert not unclassified, (
        f"задачи вне классификации: {unclassified}. Реши, источник это или "
        "обслуживание: источнику нужен heartbeat и проверка свежести"
    )


def test_classification_lists_do_not_overlap():
    """Задача либо источник, либо обслуживание — не то и другое сразу."""
    both = sorted(_DATA_SOURCES & _MAINTENANCE)
    assert not both, both


# ── Прогон без единого ответа источника — не успех ──────────────────────────
#
# Надзор выше отвечает на вопрос «прогон был». Он ничего не знает про то,
# получил ли прогон данные, а `_record_beat_heartbeat` пишет heartbeat на
# любой SUCCESS без error-маркера в retval. Значит источник, который ходит и
# каждый раз возвращает пустоту, для всего надзора выглядит здоровым.
#
# Замер 05.09.2026: `kg_statics_versions_sync` пятнадцатые сутки отдавал
# `observed=0` (NetworkPolicy не пускала на порт statics-Postgres), heartbeat
# писался каждые 5 минут, `check_sync_lag` показывал `ok`.

def _run_statics_sync(observe_result):
    from unittest.mock import patch
    from app.workers import tasks as m

    with patch("app.services.statics_service.observe_statics_version",
               return_value=observe_result), \
         patch.object(m.settings, "STATICS_TRACK_ENVS", "prod,preprod"):
        return m.kg_statics_versions_sync_task()


def _heartbeat_would_be_written(retval) -> bool:
    """Тот же предикат, что в `_record_beat_heartbeat`: контракт источника."""
    from app.knowledge_graph.source_status import HEARTBEAT_STATUSES, status_of

    return status_of(retval) in HEARTBEAT_STATUSES


def test_statics_sync_without_a_single_answer_is_not_a_success():
    """Ни один env не ответил — heartbeat писать нельзя.

    Иначе единственный надзор за источником (heartbeat + sync_lag поверх
    него) подтверждает здоровье ровно в тот момент, когда источник мёртв.
    В #350 это делалось error-маркером; теперь — статусом EMPTY: это не
    сбой задачи, а ответ источника, и называть его надо своим именем.
    """
    from app.knowledge_graph.source_status import SourceStatus, status_of

    res = _run_statics_sync(None)

    assert res["observed"] == 0
    assert status_of(res) is SourceStatus.EMPTY
    assert not _heartbeat_would_be_written(res)


def test_statics_sync_with_answers_still_writes_heartbeat():
    """Источник ответил — прогон успешный, поведение прежнее."""
    from app.knowledge_graph.source_status import SourceStatus, status_of

    res = _run_statics_sync({"version": 10175, "prev_version": None})

    assert res["observed"] == 2
    assert status_of(res) is SourceStatus.SUCCESS
    assert _heartbeat_would_be_written(res)


# ── Контракт ответа источника ───────────────────────────────────────────────
#
# Heartbeat отвечает на вопрос «прогон был». Он ничего не знает про то,
# получил ли прогон данные, — и до 05.09.2026 писался на любой SUCCESS без
# error-маркера. Задача, которая ходит и каждый раз возвращается с пустыми
# руками, для надзора выглядела здоровой.
#
# Теперь каждая задача-источник обязана вернуть `source_status`
# (see source_status.py), а heartbeat пишется только на SUCCESS/PARTIAL.
# Проверка статическая: если у задачи нет вызова одной из трёх обёрток,
# её retval уйдёт без статуса и heartbeat запишется по старому правилу.

_STATUS_WRAPPERS = ("_src_status(", "_src_polled(", "_src_seq(")


def _task_source(task_name: str) -> str:
    import inspect

    from app.workers import tasks as m

    for obj in vars(m).values():
        name = getattr(obj, "name", None)
        if name == task_name and callable(obj):
            fn = getattr(obj, "run", None) or getattr(obj, "__wrapped__", None) or obj
            try:
                return inspect.getsource(fn)
            except (OSError, TypeError):
                continue
    raise AssertionError(f"задача {task_name} не найдена в app.workers.tasks")


@pytest.mark.parametrize("task", sorted(_DATA_SOURCES))
def test_every_data_source_declares_its_status(task):
    """retval без `source_status` — heartbeat по старому правилу, то есть
    источник снова может пятнадцать суток отдавать пустоту и считаться живым.
    """
    src = _task_source(task)
    assert any(w in src for w in _STATUS_WRAPPERS), (
        f"{task} не проставляет source_status: оберни return в _src_status / "
        "_src_polled / _src_seq (см. source_status.py)"
    )
