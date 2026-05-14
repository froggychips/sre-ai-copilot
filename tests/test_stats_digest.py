"""Тесты на stats_digest — pure data-aggregation, без LLM.

Покрываем рендеринг каждой секции на моках, плюс end-to-end build_digest
с in-memory state. Invariant-тест что модуль НЕ импортит LLM-классы —
в отдельном файле test_stats_digest_no_llm.py.
"""
from collections import Counter
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from app.services import stats_digest


# ── ns_to_team_map: business-team приоритетнее platform ─────────────────────

def test_ns_to_team_map_prefers_business_team_over_platform():
    """В KG в одном namespace могут быть и kingdom1 services, и platform
    (synthetic NATS-узлы). MIN()-filter не должен схватить platform."""
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [
        ("prod-kingdom1", "kingdom1"),
        ("prod-shared", "shared"),
        ("preprod-kingdom2", "kingdom2"),
    ]
    result = stats_digest._get_ns_to_team_map(db)
    assert result == {
        "prod-kingdom1": "kingdom1",
        "prod-shared": "shared",
        "preprod-kingdom2": "kingdom2",
    }


# ── cluster_health_section ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cluster_health_renders_with_data():
    vm = MagicMock()
    vm.get_cluster_health = AsyncMock(return_value=MagicMock(
        to_dict=lambda: {"nodes_ready": 16, "crashloops": 8, "firing_alerts": 47}
    ))
    text = await stats_digest.cluster_health_section(vm, fired_series=[{}] * 47)
    assert "Cluster Health" in text
    assert "`16`" in text  # nodes
    assert "`8`" in text   # crashloops
    assert "`47`" in text  # series count


@pytest.mark.asyncio
async def test_cluster_health_graceful_on_vm_error():
    vm = MagicMock()
    vm.get_cluster_health = AsyncMock(side_effect=RuntimeError("VM unreachable"))
    text = await stats_digest.cluster_health_section(vm, fired_series=[])
    assert "`?`" in text  # nodes fallback


# ── firing_alerts_section ──────────────────────────────────────────────────

def test_firing_alerts_groups_by_team_via_kg():
    fired = [
        {"metric": {"namespace": "prod-kingdom1", "alertname": "KubePodCrashLooping"}},
        {"metric": {"namespace": "prod-kingdom1", "alertname": "CPUThrottlingHigh"}},
        {"metric": {"namespace": "prod-shared", "alertname": "KubePodNotReady"}},
        {"metric": {"namespace": "monitoring", "alertname": "InfoInhibitor"}},  # unowned
    ]
    ns_to_team = {
        "prod-kingdom1": "kingdom1",
        "prod-shared": "shared",
        # monitoring → нет
    }
    text, unique, team_alerts = stats_digest.firing_alerts_section(fired, ns_to_team)
    assert "@kingdom1" in text
    assert "@shared" in text
    assert "monitoring=1" in text  # unowned секция
    assert unique["KubePodCrashLooping"] == 1
    assert unique["CPUThrottlingHigh"] == 1
    assert team_alerts["kingdom1"] == 2
    assert team_alerts["shared"] == 1


def test_firing_alerts_empty_state_message():
    text, unique, team_alerts = stats_digest.firing_alerts_section([], {})
    assert "кластер здоров" in text
    assert len(unique) == 0


# ── top_alert_types ────────────────────────────────────────────────────────

def test_top_alert_types_top_5():
    counter = Counter({
        "KubePodCrashLooping": 30,
        "CPUThrottlingHigh": 25,
        "KubeJobFailed": 8,
        "ScrapePoolHasNoTargets": 3,
        "FifthOne": 2,
        "SixthShouldBeHidden": 1,
    })
    text = stats_digest.top_alert_types_section(counter)
    assert "KubePodCrashLooping" in text
    assert "× 30" in text
    assert "SixthShouldBeHidden" not in text  # лимит 5


def test_top_alert_types_empty():
    text = stats_digest.top_alert_types_section(Counter())
    assert "нет активных алертов" in text


# ── fragile_services_section ───────────────────────────────────────────────

def test_fragile_services_renders_top_callers():
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [
        ("auth-service", "prod-shared", 5),
        ("town-service", "prod-kingdom1", 3),
        ("dev-service", "prod-kingdom2", 2),
    ]
    text = stats_digest.fragile_services_section(
        db, ns_to_team={"prod-shared": "shared", "prod-kingdom1": "kingdom1", "prod-kingdom2": "kingdom2"},
    )
    assert "auth-service" in text
    assert "5 callers" in text
    assert "@shared" in text
    assert "@kingdom1" in text


def test_fragile_services_empty_state():
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []
    text = stats_digest.fragile_services_section(db, ns_to_team={})
    assert "нет edges" in text


# ── stale_deployments_section ──────────────────────────────────────────────

def _make_deployment(name: str, last_update_iso: str, replicas: int = 1) -> dict:
    return {
        "metadata": {
            "name": name,
            "annotations": {"meta.helm.sh/release-name": name},
            "creationTimestamp": last_update_iso,
        },
        "status": {
            "readyReplicas": replicas,
            "conditions": [{"lastUpdateTime": last_update_iso, "type": "Available", "status": "True"}],
        },
    }


def test_stale_deployments_groups_by_team_and_excludes_fresh():
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [("prod-kingdom1",), ("prod-kingdom2",)]

    now = datetime.now(timezone.utc)
    fake_deploys = {
        "prod-kingdom1": [
            _make_deployment("town-service", (now - timedelta(days=30)).isoformat()),
            _make_deployment("fresh-deploy", (now - timedelta(days=1)).isoformat()),
        ],
        "prod-kingdom2": [
            _make_deployment("legacy-cron", (now - timedelta(days=90)).isoformat()),
        ],
    }
    text = stats_digest.stale_deployments_section(
        db,
        ns_to_team={"prod-kingdom1": "kingdom1", "prod-kingdom2": "kingdom2"},
        threshold_days=14,
        kubectl_fn=lambda ns: fake_deploys.get(ns, []),
    )
    assert "town-service" in text
    assert "legacy-cron" in text
    assert "fresh-deploy" not in text  # под 14d threshold
    assert "@kingdom1" in text
    assert "@kingdom2" in text


def test_stale_deployments_ignores_zero_replicas():
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [("prod-kingdom1",)]
    now = datetime.now(timezone.utc)
    fake = _make_deployment("scaled-zero", (now - timedelta(days=100)).isoformat(), replicas=0)
    text = stats_digest.stale_deployments_section(
        db, ns_to_team={"prod-kingdom1": "kingdom1"}, threshold_days=14,
        kubectl_fn=lambda ns: [fake],
    )
    assert "scaled-zero" not in text
    assert "ничего не stale" in text


def test_stale_deployments_caps_total_at_15():
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [("prod-kingdom1",)]
    now = datetime.now(timezone.utc)
    many = [_make_deployment(f"dep-{i}", (now - timedelta(days=30+i)).isoformat()) for i in range(25)]
    text = stats_digest.stale_deployments_section(
        db, ns_to_team={"prod-kingdom1": "kingdom1"}, threshold_days=14,
        kubectl_fn=lambda ns: many,
    )
    assert "и ещё 10 (скрыто)" in text


# ── kg_quality_section ─────────────────────────────────────────────────────

def test_kg_quality_renders_full_state():
    """Order of execute-вызовов в kg_quality_section:
       1. services_total .scalar
       2. edges_total .scalar
       3. edges_by_kind .fetchall
       4. orphan .scalar
       5. teamed .scalar
       6. by_team .fetchall
    """
    db = MagicMock()
    call_results = [
        MagicMock(scalar=lambda: 363),
        MagicMock(scalar=lambda: 712),
        MagicMock(fetchall=lambda: [("calls", 36), ("uses_nats", 676)]),
        MagicMock(scalar=lambda: 47),
        MagicMock(scalar=lambda: 348),
        MagicMock(fetchall=lambda: [("kingdom1", 24), ("kingdom2", 24), ("shared", 26)]),
    ]
    db.execute.side_effect = call_results

    rendered = stats_digest.kg_quality_section(db)
    assert "`363`" in rendered
    assert "`47`" in rendered  # orphan
    assert "(12%)" in rendered or "(13%)" in rendered  # 47/363 ≈ 12.94%
    assert "calls=36" in rendered
    assert "uses_nats=676" in rendered
    assert "@kingdom1" in rendered


def test_kg_quality_empty_kg():
    db = MagicMock()
    db.execute.return_value.scalar.return_value = 0
    text = stats_digest.kg_quality_section(db)
    assert "KG пустой" in text


# ── _last_update fallback ──────────────────────────────────────────────────

def test_last_update_from_conditions():
    dep = {
        "metadata": {"creationTimestamp": "2026-01-01T00:00:00Z"},
        "status": {"conditions": [
            {"lastUpdateTime": "2026-04-01T12:00:00Z"},
            {"lastUpdateTime": "2026-05-01T12:00:00Z"},  # max
        ]},
    }
    dt = stats_digest._last_update(dep)
    assert dt is not None
    assert dt.month == 5  # max выбран


def test_last_update_fallback_creation_timestamp():
    dep = {
        "metadata": {"creationTimestamp": "2026-01-01T00:00:00Z"},
        "status": {"conditions": []},
    }
    dt = stats_digest._last_update(dep)
    assert dt is not None
    assert dt.year == 2026 and dt.month == 1


def test_last_update_returns_none_when_no_data():
    assert stats_digest._last_update({"metadata": {}, "status": {}}) is None


# ── send_daily_digest gate ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_daily_digest_skips_when_disabled():
    with patch.object(stats_digest.settings, "STATS_DIGEST_ENABLED", False):
        result = await stats_digest.send_daily_digest(db=MagicMock())
    assert result == {"status": "skipped", "reason": "disabled"}


@pytest.mark.asyncio
async def test_send_daily_digest_sends_when_enabled():
    fake_discord = MagicMock()
    fake_discord.send_stats_report = AsyncMock()
    with patch.object(stats_digest.settings, "STATS_DIGEST_ENABLED", True), \
         patch.object(stats_digest.settings, "VICTORIA_METRICS_URL", ""), \
         patch.object(stats_digest, "build_digest", new=AsyncMock(return_value="DIGEST_BODY")), \
         patch("app.services.discord_service.discord_service", fake_discord):
        result = await stats_digest.send_daily_digest(db=MagicMock())
    assert result["status"] == "sent"
    fake_discord.send_stats_report.assert_awaited_once_with("DIGEST_BODY")
