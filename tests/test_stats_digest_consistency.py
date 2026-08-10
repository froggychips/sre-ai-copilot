"""Вторая волна кодревью дайджеста: мёртвый skip-noop, единицы, пороги.

Каждый тест держит один класс находок — все они про «дайджест утверждает не
то, что посчитал», либо про механизм, который выглядит рабочим, но никогда не
срабатывает:

  * C1 — skip-if-noop был МЁРТВЫМ кодом. `changes_section` возвращает
    непустой текст всегда (даже «+0 new alerts · -0 resolved»), поэтому
    `sections_with_content` никогда не был нулём и инвариант A2 («пустой
    digest вообще не постится») не работал: дайджест уходил в #stats каждый
    день, включая полностью тихие. Существующие тесты подсовывали
    `sections_with_content=0` мокой — то есть проверяли ветку, до которой
    реальная сборка не доходила. Здесь она достигается по-настоящему.

  * C2 — секции без изоляции. `_get_ns_to_team_map` (первый вызов сборки),
    `kg_quality_section` и первый запрос `stale_deployments_section` не
    ловили ошибок БД: транзиентный сбой ронял ВСЮ сборку, а не одну секцию.

  * C3 — три разных порога «chronic» (3/5/10) в одном сообщении, все три
    подписаны одним словом. Числа не сходились ни между собой, ни с
    6-часовым chronic-дайджестом, и сверить их читатель не мог.

  * C4 — deploy-correlation врал в единицах: «attributed alerts» — это на
    деле раскатки с ≥1 алертом, «alerts in 30m» — окно [-5m; finished+60m],
    а сам матч шёл по `last_notified_at`, тогда как его же диагностика
    считала по `COALESCE(last_notified_at, fired_at)`.

  * C5 — noisemakers: заголовок «(24h)» над пятиминутным снимком, счётчик по
    одному имени сервиса без namespace, и `pod.rsplit("-", 2)[0]`, который
    калечит имена StatefulSet-подов.

  * C6 — new-baseline ветка `changes_section` рендерила литеральное `None`.

Всё на моках, без живого PostgreSQL и без VM.
"""
from __future__ import annotations

from collections import Counter
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import stats_digest
from app.services.stats_digest import ChangeReport


# ── общий стенд «тихий день» ────────────────────────────────────────────────


def _empty_db() -> MagicMock:
    """Session-мок, отвечающий «пусто» на любой запрос секции."""
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []
    db.execute.return_value.fetchone.return_value = None
    db.execute.return_value.scalar.return_value = 0
    db.query.return_value.filter.return_value.all.return_value = []
    return db


def _quiet_day_patches(*, day_snapshot=None, topology_snapshot=None):
    """Патчи, делающие сборку дайджеста детерминированной и «тихой».

    Redis/VM/kubectl/TC — наружу не ходим; всё остальное считается настоящими
    секциями, иначе тест не доказывал бы достижимость skip-noop.
    """
    snap = day_snapshot if day_snapshot is not None else {
        "firing_series": 0, "crashloops": None, "kg_edges": 0, "kg_services": 0,
    }
    topo = topology_snapshot if topology_snapshot is not None else {
        "services": 0, "edges": 0, "nats_subjects": [],
    }
    return [
        patch.object(stats_digest.settings, "VICTORIA_METRICS_URL", ""),
        patch.object(stats_digest, "_read_last_firing_series",
                     new=AsyncMock(return_value=0)),
        patch.object(stats_digest, "_read_day_snapshot",
                     new=AsyncMock(return_value=snap)),
        patch.object(stats_digest, "_write_last_firing_series",
                     new=AsyncMock(return_value=None)),
        patch.object(stats_digest, "_write_day_snapshot",
                     new=AsyncMock(return_value=None)),
        patch.object(stats_digest, "_read_topology_snapshot",
                     new=AsyncMock(return_value=topo)),
        patch.object(stats_digest, "_write_topology_snapshot",
                     new=AsyncMock(return_value=None)),
        patch.object(stats_digest, "recent_deploys_section",
                     new=AsyncMock(return_value="")),
        patch.object(stats_digest, "_kubectl_get_deployments_json",
                     return_value=[]),
        patch.object(stats_digest, "_metrics_sync_lag_minutes",
                     return_value=None),
        patch.object(stats_digest, "pipeline_health_section", return_value=""),
        patch.object(stats_digest, "beat_heartbeats_footer", return_value=""),
    ]


async def _build_quiet(db, **kwargs):
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in _quiet_day_patches(**kwargs):
            stack.enter_context(p)
        return await stats_digest._build_digest_with_meta(db)


# ── C1. skip-if-noop реально достижим ──────────────────────────────────────


def test_changes_section_never_returns_empty_string():
    """Фиксируем причину бага: текст секции непустой даже при нулевом Δ.

    Именно поэтому skip-noop нельзя считать по тексту — только по данным.
    """
    text = stats_digest.changes_section(ChangeReport(new_baseline=False))
    assert text != ""
    assert "new alerts" in text


def test_change_report_has_no_signal_when_nothing_happened():
    assert stats_digest.change_report_has_signal(
        ChangeReport(new_baseline=False, kg_edges_today=0, kg_edges_yesterday=0)
    ) is False


@pytest.mark.parametrize("report", [
    ChangeReport(new_alerts_24h=1),
    ChangeReport(resolved_alerts_24h=1),
    ChangeReport(chronic_in_new=1),
    ChangeReport(nats_subjects_new=["events.refresh"]),
    ChangeReport(kg_edges_today=10, kg_edges_yesterday=9),
    ChangeReport(kg_services_today=10, kg_services_yesterday=9),
])
def test_change_report_has_signal_for_any_real_change(report):
    assert stats_digest.change_report_has_signal(report) is True


def test_new_baseline_alone_is_not_a_signal():
    """Отсутствие снапшота — не событие в кластере.

    Иначе первый прогон после Redis-flush постился бы пустым дайджестом.
    """
    assert stats_digest.change_report_has_signal(
        ChangeReport(new_baseline=True)
    ) is False


@pytest.mark.asyncio
async def test_quiet_day_really_yields_zero_actionable_sections():
    """РЕАЛЬНАЯ достижимость: секции считаются, а не подсовываются мокой."""
    content, meta = await _build_quiet(_empty_db())

    assert meta["sections_with_content"] == 0, (
        "skip-noop опять недостижим — какая-то секция считается непустой:\n"
        + content
    )
    assert meta["failed_sections"] == []
    # Сборка при этом доходит до конца и остаётся читаемой.
    assert "Cluster Daily Digest" in content


@pytest.mark.asyncio
async def test_send_daily_digest_skips_noop_without_mocking_meta():
    """End-to-end: тихий день → в Discord не уходит ничего."""
    from contextlib import ExitStack

    fake_discord = MagicMock()
    fake_discord.send_stats_report = AsyncMock(return_value=True)
    with ExitStack() as stack:
        for p in _quiet_day_patches():
            stack.enter_context(p)
        stack.enter_context(
            patch.object(stats_digest.settings, "STATS_DIGEST_ENABLED", True)
        )
        stack.enter_context(patch.object(
            stats_digest.settings, "STATS_DIGEST_SKIP_NOOP", True, create=True
        ))
        stack.enter_context(
            patch("app.services.discord_service.discord_service", fake_discord)
        )
        result = await stats_digest.send_daily_digest(db=_empty_db())

    assert result["status"] == "skipped_noop"
    fake_discord.send_stats_report.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_new_alert_makes_the_digest_postable_again():
    """Один реальный алерт за окно → skip-noop не срабатывает.

    Обратная сторона фикса: «тихо» должно означать именно тишину, а не
    «мы разучились считать».
    """
    db = _empty_db()
    with patch.object(stats_digest, "_count_alerts_in_window",
                      return_value=(1, 0)):
        content, meta = await _build_quiet(db)

    assert meta["sections_with_content"] >= 1
    assert "`+1` new alerts" in content


# ── C2. падение секции не роняет сборку ────────────────────────────────────


def test_ns_to_team_map_survives_db_error_and_marks_failure():
    """Карта строится ПЕРВОЙ — её исключение раньше уносило весь дайджест."""
    stats_digest._reset_section_failures()
    db = MagicMock()
    db.execute.side_effect = RuntimeError("could not connect")

    assert stats_digest._get_ns_to_team_map(db) == {}
    assert "_get_ns_to_team_map" in stats_digest.section_failures_line()


def test_kg_quality_section_survives_db_error_and_marks_failure():
    stats_digest._reset_section_failures()
    db = MagicMock()
    db.execute.side_effect = RuntimeError("relation does not exist")

    assert stats_digest.kg_quality_section(db) == ""
    assert "kg_quality_section" in stats_digest.section_failures_line()


def test_stale_deployments_survives_db_error_and_marks_failure():
    """Первый запрос секции (список namespace-ов) тоже обёрнут."""
    stats_digest._reset_section_failures()
    db = MagicMock()
    db.execute.side_effect = RuntimeError("InFailedSqlTransaction")

    out = stats_digest.stale_deployments_section(
        db, {}, threshold_days=30, kubectl_fn=lambda ns: [],
    )
    assert out == ""
    assert "stale_deployments_section" in stats_digest.section_failures_line()


@pytest.mark.asyncio
async def test_build_survives_totally_broken_db_and_says_so():
    """Ни один SQL не проходит — дайджест всё равно собирается и жалуется."""
    db = MagicMock()
    db.execute.side_effect = RuntimeError("server closed the connection")
    db.query.side_effect = RuntimeError("server closed the connection")

    content, meta = await _build_quiet(db)

    assert "Cluster Daily Digest" in content
    assert "Секции недоступны" in content
    for section in ("_get_ns_to_team_map", "kg_quality_section",
                    "stale_deployments_section"):
        assert section in meta["failed_sections"], meta["failed_sections"]


@pytest.mark.asyncio
async def test_failure_warning_is_rendered_before_the_body():
    """Строка самодиагностики стоит выше секций — её не должна съесть обрезка."""
    db = MagicMock()
    db.execute.side_effect = RuntimeError("boom")
    db.query.side_effect = RuntimeError("boom")

    content, _ = await _build_quiet(db)

    assert content.index("Секции недоступны") < content.index("Cluster Health")


# ── C3. пороги chronic подписаны и не путаются ─────────────────────────────


def test_chronic_thresholds_are_named_and_ordered():
    """Три порога — три константы, и порядок «повтор < трек < RCA» осмыслен."""
    assert stats_digest.CHRONIC_REPEAT_MIN_FIRES == 3
    assert stats_digest.CHRONIC_TRACKED_MIN_FIRES == 5
    assert stats_digest.CHRONIC_RCA_MIN_FIRES == 10
    assert (
        stats_digest.CHRONIC_REPEAT_MIN_FIRES
        < stats_digest.CHRONIC_TRACKED_MIN_FIRES
        < stats_digest.CHRONIC_RCA_MIN_FIRES
    )


def test_changes_section_states_its_chronic_threshold():
    text = stats_digest.changes_section(ChangeReport(
        new_alerts_24h=12, chronic_in_new=5, resolved_alerts_24h=1,
    ))
    assert (
        f"≥{stats_digest.CHRONIC_TRACKED_MIN_FIRES} fires/"
        f"{stats_digest.CHRONIC_WINDOW_HOURS}h"
    ) in text


def test_top_alert_types_states_its_chronic_threshold_and_unit():
    """Тут «chronic» — СЕРВИСЫ с ≥3 fires, и это должно быть написано."""
    db = MagicMock()
    db.execute.side_effect = [
        MagicMock(scalar=lambda: True),   # есть история за yesterday-окно
        MagicMock(scalar=lambda: True),   # есть история за today-окно
        MagicMock(fetchall=lambda: [("KubePodCrashLooping", 10)]),   # yest
        MagicMock(fetchall=lambda: [("KubePodCrashLooping", 12)]),   # today
        MagicMock(fetchall=lambda: [("KubePodCrashLooping", 4)]),    # chronic
        MagicMock(fetchall=lambda: []),                              # resurf
    ]
    text = stats_digest.top_alert_types_section(
        Counter({"KubePodCrashLooping": 12}), db
    )
    assert (
        f"4 chronic svc ≥{stats_digest.CHRONIC_REPEAT_MIN_FIRES}/"
        f"{stats_digest.CHRONIC_WINDOW_HOURS}h"
    ) in text


def test_top_alert_types_chronic_query_uses_the_named_threshold():
    """Порог уехал в bind-параметр — число в SQL и в тексте одно и то же."""
    db = MagicMock()
    db.execute.side_effect = [
        MagicMock(scalar=lambda: True),
        MagicMock(scalar=lambda: True),
        MagicMock(fetchall=lambda: []),
        MagicMock(fetchall=lambda: []),
        MagicMock(fetchall=lambda: []),
        MagicMock(fetchall=lambda: []),
    ]
    stats_digest._alert_type_metadata(db, ["X"])
    params = [c.args[1] for c in db.execute.call_args_list if len(c.args) > 1]
    chronic_params = [p for p in params if "chronic_min" in p]
    assert chronic_params, "порог chronic не передан параметром"
    assert chronic_params[0]["chronic_min"] == stats_digest.CHRONIC_REPEAT_MIN_FIRES


def test_action_items_states_its_chronic_threshold():
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [
        ("svc-a", "prod-x", "AlertX", 15),
    ]
    db.execute.return_value.scalar.side_effect = [0, 0]
    text = stats_digest.action_items_section(db)
    assert f"≥{stats_digest.CHRONIC_RCA_MIN_FIRES} fires/24h" in text


# ── C4. deploy-correlation: единицы и окно ─────────────────────────────────


def _corr_db(overall, worst=None):
    db = MagicMock()
    db.execute.return_value.fetchone.side_effect = [overall, worst]
    return db


def test_deploy_correlation_counts_rollouts_not_alerts():
    """`attributed` = раскатки с алертом, а не алерты — так и подписываем."""
    text = stats_digest.deploy_incident_correlation_section(
        _corr_db((795, 40, 795, 265, 12)), hours=24
    )
    assert "attributed alerts" not in text
    assert "`40` rollouts с алертами в окне" in text
    assert "5% раскаток" in text  # 40/795 = 5%


def test_deploy_correlation_names_the_real_window_everywhere():
    text = stats_digest.deploy_incident_correlation_section(
        _corr_db((18, 7, 11, 9, 2), ("2138", "wizaryx", 3)), hours=24
    )
    assert "in 30m" not in text
    assert text.count("[-5m; finished+60m]") >= 2  # overall + Worst


def test_deploy_correlation_match_agrees_with_its_diagnostics():
    """Матч и диагностика обязаны смотреть на одну популяцию алертов.

    Раньше матч брал только `last_notified_at` (алерт с NULL в этой колонке в
    матче не участвовал), а диагностика — `COALESCE(last_notified_at,
    fired_at)`, и на attributed=0 докладывала «привязка целая» про алерты,
    которых матч не видел.
    """
    db = _corr_db((18, 7, 11, 9, 2), None)
    stats_digest.deploy_incident_correlation_section(db, hours=24)
    corr_sql = " ".join(str(c.args[0]) for c in db.execute.call_args_list)

    diag_db = MagicMock()
    diag_db.execute.return_value.fetchone.return_value = (1, 1, 1, 1, 1, 1, 1)
    stats_digest._deploy_correlation_diagnostics(diag_db, hours=24)
    diag_sql = " ".join(str(c.args[0]) for c in diag_db.execute.call_args_list)

    assert "COALESCE(a.last_notified_at, a.fired_at)" in corr_sql
    assert "COALESCE(last_notified_at, fired_at)" in diag_sql
    # ни один путь не матчит по «сырому» last_notified_at
    assert "a.last_notified_at BETWEEN" not in corr_sql


# ── C5. noisemakers: окно, namespace, имена подов ──────────────────────────


def _series(n, **labels):
    return [{"metric": dict(labels)}] * n


def test_noisemakers_header_states_the_real_window():
    """Данные — снимок firing-серий за 5 минут, а не сутки."""
    text = stats_digest.noisemakers_section(
        _series(30, namespace="prod-shared", service="bot"), threshold_pct=20.0
    )
    assert "24h" not in text
    assert "5m" in text
    # и единица — серии, не «events за сутки»
    assert "firing-серий" in text


def test_noisemakers_group_by_namespace_keeps_same_name_apart():
    """Одноимённые сервисы разных ns — разные строки, а не один «шумный bot».

    Без ns счётчик схлопывал их в 80%, а `ns_map.setdefault` подписывал сумму
    первым попавшимся namespace — строка обвиняла не то окружение.
    """
    fired = (
        _series(40, namespace="squad-1-kingdom1", service="bot")
        + _series(40, namespace="squad-2-kingdom1", service="bot")
        + _series(20, namespace="prod-shared", service="quiet")
    )
    text = stats_digest.noisemakers_section(fired, threshold_pct=20.0)

    assert "@squad-1-kingdom1" in text
    assert "@squad-2-kingdom1" in text
    assert text.count("`bot`") == 2
    assert "`80%`" not in text
    assert text.count("`40%`") == 2


def test_noisemakers_statefulset_pod_keeps_full_service_name():
    """`clickhouse-keeper-0` → `clickhouse-keeper`, а не `clickhouse`."""
    fired = [
        {"metric": {"namespace": "prod-shared", "pod": f"clickhouse-keeper-{i}"}}
        for i in range(3)
    ] * 10
    text = stats_digest.noisemakers_section(fired, threshold_pct=20.0)

    assert "`clickhouse-keeper`" in text
    assert "`clickhouse`" not in text


@pytest.mark.parametrize("pod,expected", [
    # StatefulSet — только порядковый номер
    ("clickhouse-keeper-0", "clickhouse-keeper"),
    ("town-db-postgresql-12", "town-db-postgresql"),
    # Deployment — rs-hash + суффикс пода
    ("town-service-7d4f8b9c5d-x9k2p", "town-service"),
    # DaemonSet/Job — один сгенерированный хвост
    ("node-exporter-9k2px", "node-exporter"),
    # Формат не распознан → имя как есть (лишний хвост честнее обрубка)
    ("standalone", "standalone"),
    ("service-with-real-suffix", "service-with-real-suffix"),
])
def test_owner_name_from_pod(pod, expected):
    assert stats_digest._owner_name_from_pod(pod) == expected


# ── C6. литеральный None не уезжает в Discord ─────────────────────────────


def test_new_baseline_branch_does_not_render_literal_none():
    """kg_edges_today=None → `?`, а не «`None` KG edges»."""
    text = stats_digest.changes_section(ChangeReport(
        new_baseline=True, new_alerts_24h=0, resolved_alerts_24h=0,
        kg_edges_today=None,
    ))
    assert "None" not in text, text
    assert "`?` KG edges" in text
    assert "new baseline" in text


def test_new_baseline_branch_still_shows_known_edge_count():
    text = stats_digest.changes_section(ChangeReport(
        new_baseline=True, kg_edges_today=1500,
    ))
    assert "`1500` KG edges" in text
