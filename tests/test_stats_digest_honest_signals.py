"""Регрессия: дайджест не должен утверждать больше, чем знает.

Три места, где он вводил читателя в заблуждение (разбор дайджеста 06.08.2026):

  * H1 — «Crashloops: None». `VMClient` намеренно различает 0.0 и None
    («нет данных»), но секция брала `d.get("crashloops", "?")`, а дефолт не
    срабатывает, если ключ ЕСТЬ со значением None. В дайджест уходило
    литеральное `None`, которое читается как «крашлупов нет» — при том что
    рядом trend-строка показывала `crashloops avg 40→35`. Обе метрики
    снапшота приходят из kube-state-metrics, поэтому когда KSM перерастает
    vmagent-овый maxScrapeSize, снапшот противоречит сам себе.

  * H2 — одно мёртвое окружение прячется в сумме по команде. squad-8 лежал
    43 часа в ImagePullBackOff и дал 66 из 77 серий двух топовых типов
    алертов, но видно было только `@external 469 series`.

  * H3 — «1060 deploys» при 4 реальных сборках (одна строка kg_deployments =
    один раскатанный СЕРВИС), и ветка диагностики attributed=0 выбиралась по
    `overlap == 0`, из-за чего при 100%/100% привязки и overlap=1 печаталось
    «⚠️ Likely linkage gap» — диагностика противоречила своим же числам.

Все тесты на моках, без живого PostgreSQL и без VM.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import stats_digest


# ── H1. None из VM = «не знаем», а не «ноль» ────────────────────────────────

@pytest.mark.asyncio
async def test_cluster_health_renders_question_mark_not_literal_none():
    """crashloops=None → `?`, а НЕ строка `None` в тексте секции."""
    vm = MagicMock()
    vm.get_cluster_health = AsyncMock(return_value=MagicMock(
        to_dict=lambda: {"nodes_ready": 55, "crashloops": None}
    ))
    text = await stats_digest.cluster_health_section(vm, fired_series=[{}] * 535)

    assert "None" not in text, f"литеральное None утекло в дайджест: {text}"
    assert "Crashloops: `?`" in text
    assert "`55`" in text


@pytest.mark.asyncio
async def test_cluster_health_names_the_broken_source():
    """При отсутствующей метрике пишем, ЧТО чинить, и что `?` ≠ ноль."""
    vm = MagicMock()
    vm.get_cluster_health = AsyncMock(return_value=MagicMock(
        to_dict=lambda: {"nodes_ready": 55, "crashloops": None}
    ))
    text = await stats_digest.cluster_health_section(vm, fired_series=[])

    assert "snapshot неполный" in text
    assert "kube-state-metrics" in text
    assert "НЕ «ноль»" in text


@pytest.mark.asyncio
async def test_cluster_health_no_warning_when_snapshot_complete():
    """Полный снапшот (включая честный ноль) — без предупреждения."""
    vm = MagicMock()
    vm.get_cluster_health = AsyncMock(return_value=MagicMock(
        to_dict=lambda: {"nodes_ready": 55, "crashloops": 0}
    ))
    text = await stats_digest.cluster_health_section(vm, fired_series=[])

    assert "snapshot неполный" not in text
    assert "Crashloops: `0`" in text


def test_fmt_snapshot_metric_keeps_zero_distinct_from_missing():
    """Ключевой инвариант контракта VMClient: 0 и None — разные вещи."""
    assert stats_digest._fmt_snapshot_metric(0) == "0"
    assert stats_digest._fmt_snapshot_metric(0.0) == "0"
    assert stats_digest._fmt_snapshot_metric(None) == "?"
    assert stats_digest._fmt_snapshot_metric(35.4) == "35"


# ── H2. Шумный namespace виден отдельно от суммы по команде ─────────────────

def test_firing_alerts_surfaces_worst_namespace():
    """squad-8 (66 серий) не должен растворяться в `@external 469`."""
    fired = (
        [{"metric": {"namespace": "squad-8-kingdom2", "alertname": "KubePodNotReady"}}] * 21
        + [{"metric": {"namespace": "squad-8-kingdom2", "alertname": "KubeDeploymentReplicasMismatch"}}] * 17
        + [{"metric": {"namespace": "squad-8-shared", "alertname": "KubePodNotReady"}}] * 14
        + [{"metric": {"namespace": "prod-kingdom2", "alertname": "Whatever"}}] * 3
    )
    ns_to_team = {
        "squad-8-kingdom2": "external",
        "squad-8-shared": "external",
        "prod-kingdom2": "external",
    }
    text, _, team_alerts, _ = stats_digest.firing_alerts_section(fired, ns_to_team)

    # Сумма по команде осталась как была.
    assert team_alerts["external"] == 55
    # И при этом видно, ГДЕ горит.
    assert "squad-8-kingdom2" in text
    assert "38" in text  # 21 + 17 — крупнейший namespace


def test_firing_alerts_worst_ns_line_does_not_break_inline_team_line():
    """Новая строка не считается body-строкой teams (префикс не `@)."""
    fired = [
        {"metric": {"namespace": f"prod-kingdom{i}", "alertname": "X"}}
        for i in range(1, 6)
        for _ in range(3)
    ]
    ns_to_team = {f"prod-kingdom{i}": f"kingdom{i}" for i in range(1, 6)}
    text, _, _, _ = stats_digest.firing_alerts_section(fired, ns_to_team)

    body_lines = [ln for ln in text.split("\n") if ln.strip().startswith("`@")]
    assert len(body_lines) == 1, f"team-строка должна остаться одна: {body_lines}"


def test_firing_alerts_single_namespace_omits_worst_ns_line():
    """Один namespace — разбивка не нужна, она дублировала бы team-строку."""
    fired = [{"metric": {"namespace": "squad-8-shared", "alertname": "X"}}] * 5
    text, _, _, _ = stats_digest.firing_alerts_section(
        fired, {"squad-8-shared": "external"}
    )
    assert "Хуже всего" not in text


# ── H3. Честная единица счёта + три причины attributed=0 ────────────────────

def test_deploy_correlation_labels_unit_and_shows_build_count():
    """`795 service-rollouts (265 svc · 12 сборок)`, а не «795 deploys»."""
    db = MagicMock()
    db.execute.return_value.fetchone.side_effect = [
        (795, 40, 795, 265, 12),  # total, attributed, successes, svcs, builds
        None,                      # worst
    ]
    text = stats_digest.deploy_incident_correlation_section(db, hours=24)

    assert "service-rollouts" in text
    assert "`265` svc" in text
    assert "`12` сборок" in text


def test_deploy_correlation_tolerates_legacy_three_column_row():
    """Старый мок/старая схема без svcs/builds не должны ронять секцию."""
    db = MagicMock()
    db.execute.return_value.fetchone.side_effect = [(18, 7, 11), ("2138", "u", 3)]
    text = stats_digest.deploy_incident_correlation_section(db, hours=24)

    assert "18" in text
    assert "Build #2138" in text


def test_deploy_correlation_intact_linkage_with_overlap_is_not_a_gap():
    """100%/100% привязки и overlap=1 — это НЕ «Likely linkage gap»."""
    db = MagicMock()
    db.execute.return_value.fetchone.side_effect = [
        (1060, 0, 1060, 265, 4),
        None,
    ]
    diag = {
        "deploys_linked_pct": 100.0,
        "alerts_linked_pct": 100.0,
        "deploy_svc": 265,
        "alert_svc": 120,
        "overlap": 1,
    }
    with patch.object(stats_digest, "_deploy_correlation_diagnostics", return_value=diag):
        text = stats_digest.deploy_incident_correlation_section(db, hours=24)

    assert "Likely linkage gap" not in text
    assert "Привязка целая" in text
    assert "не от раскаток" in text


def test_deploy_correlation_reports_gap_when_linkage_actually_broken():
    """Низкий linked% — по-прежнему честный linkage gap."""
    db = MagicMock()
    db.execute.return_value.fetchone.side_effect = [(500, 0, 500, 200, 3), None]
    diag = {
        "deploys_linked_pct": 12.0,
        "alerts_linked_pct": 95.0,
        "deploy_svc": 200,
        "alert_svc": 80,
        "overlap": 0,
    }
    with patch.object(stats_digest, "_deploy_correlation_diagnostics", return_value=diag):
        text = stats_digest.deploy_incident_correlation_section(db, hours=24)

    assert "Likely linkage gap" in text


def test_deploy_correlation_no_overlap_still_reported_as_not_a_bug():
    """overlap=0 при целой привязке — прежняя формулировка сохранена."""
    db = MagicMock()
    db.execute.return_value.fetchone.side_effect = [(500, 0, 500, 200, 3), None]
    diag = {
        "deploys_linked_pct": 100.0,
        "alerts_linked_pct": 100.0,
        "deploy_svc": 200,
        "alert_svc": 80,
        "overlap": 0,
    }
    with patch.object(stats_digest, "_deploy_correlation_diagnostics", return_value=diag):
        text = stats_digest.deploy_incident_correlation_section(db, hours=24)

    assert "Нет пересечения" in text
    assert "не linkage-баг" in text
