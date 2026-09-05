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
