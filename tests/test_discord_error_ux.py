"""Discord error embed UX overhaul (2026-05-25).

Покрытие:
  * TL;DR: regression suspected когда deploy <30m + majority replicas down.
  * TL;DR: chronic когда recurrence_24h count > 10.
  * TL;DR: OOMKilled pattern когда ≥3 OOMKilled events.
  * TL;DR: fallback на summary первые 80 chars.
  * Runbook link: known alertname → URL, unknown → no field.
  * Severity colors mapping (critical/warning/resolved/resurfaced).
  * Mention block: @here только для critical.
  * Self-mon footer: stale KG sync → warning emoji.
  * Compact mode warning_only → one-line render (без embed.fields).
  * TC build URL clickable format (build_url > build_id > none).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.models.incident import Incident
from app.services.alert_enrichment import EnrichedContext
from app.services.discord.embed_builder import (
    SEVERITY_COLOR_CRITICAL,
    SEVERITY_COLOR_RESOLVED,
    SEVERITY_COLOR_RESURFACED,
    SEVERITY_COLOR_WARNING,
    _build_runbook_field,
    _build_tldr_field,
    _allowed_mentions,
    _is_majority_replicas_down,
    _mention_block,
    _runbook_link,
    _self_health_footer,
    _severity_to_color,
    _tc_build_url,
)
from app.services.discord_service import DiscordService


# ── pure helpers ────────────────────────────────────────────────────────


def test_severity_to_color_critical_red():
    assert _severity_to_color("critical") == SEVERITY_COLOR_CRITICAL


def test_severity_to_color_warning_yellow():
    assert _severity_to_color("warning") == SEVERITY_COLOR_WARNING


def test_severity_to_color_resurfaced_overrides_severity():
    # resurfaced > severity priority
    assert _severity_to_color("critical", resurfaced=True) == SEVERITY_COLOR_RESURFACED


def test_severity_to_color_resolved_branch():
    assert _severity_to_color("warning", resolved=True) == SEVERITY_COLOR_RESOLVED


def test_mention_block_critical_returns_here():
    assert _mention_block("critical") == "@here\n"


def test_mention_block_warning_returns_empty():
    assert _mention_block("warning") == ""
    assert _mention_block("info") == ""


def test_mention_block_critical_role_id_pings_role(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "DISCORD_ALERT_MENTION_ROLE_ID", "1425470692895228024")
    assert _mention_block("critical") == "<@&1425470692895228024>\n"
    # warning по-прежнему молчит даже с ролью
    assert _mention_block("warning") == ""


def test_allowed_mentions_for_role_and_here(monkeypatch):
    from app.config import settings
    assert _allowed_mentions("") == {"parse": []}
    assert _allowed_mentions("@here\n") == {"parse": ["everyone"]}
    monkeypatch.setattr(settings, "DISCORD_ALERT_MENTION_ROLE_ID", "1425470692895228024")
    assert _allowed_mentions("<@&1425470692895228024>\n") == {
        "parse": [],
        "roles": ["1425470692895228024"],
    }


# ── runbook link ────────────────────────────────────────────────────────


def test_runbook_link_known_alertname():
    url = _runbook_link("KubePodCrashLooping", "https://gh.com/foo/runbook.md")
    assert url == "https://gh.com/foo/runbook.md#kube-pod-crashlooping"


def test_runbook_link_unknown_returns_none():
    assert _runbook_link("MyCustomAlert", "https://gh.com/foo/runbook.md") is None


def test_runbook_link_empty_alertname_returns_none():
    assert _runbook_link(None, "https://gh.com/foo/runbook.md") is None
    assert _runbook_link("", "https://gh.com/foo/runbook.md") is None


def test_build_runbook_field_known_returns_field():
    f = _build_runbook_field("KubePodCrashLooping", "https://gh.com/r.md")
    assert f is not None
    assert f["name"] == "📖 Runbook"
    assert "KubePodCrashLooping" in f["value"]
    assert "kube-pod-crashlooping" in f["value"]


def test_build_runbook_field_unknown_returns_none():
    assert _build_runbook_field("MyAlert", "https://gh.com/r.md") is None


# ── TL;DR ───────────────────────────────────────────────────────────────


def test_tldr_regression_suspected_when_deploy_under_30m_and_majority_down():
    field = _build_tldr_field(
        summary="some text",
        pod_events=None,
        recent_deploys=[{
            "minutes_before_incident": 14,
            "number": 2126,
            "sha": "4b2daaa0deadbeef",
        }],
        replicas_ready_desired="0/3",  # 100% down
        recurrence_24h=[],
    )
    assert field is not None
    assert "regression suspected" in field["value"]
    assert "#2126" in field["value"]
    assert "4b2daaa" in field["value"]


def test_tldr_no_regression_when_deploy_old_or_replicas_up():
    # deploy 40m назад — НЕ regression
    field = _build_tldr_field(
        summary="some text",
        pod_events=None,
        recent_deploys=[{"minutes_before_incident": 40, "number": 1}],
        replicas_ready_desired="0/3",
        recurrence_24h=[],
    )
    assert field is not None
    assert "regression suspected" not in field["value"]


def test_tldr_chronic_when_count_over_10():
    field = _build_tldr_field(
        summary="x",
        pod_events=None,
        recent_deploys=None,
        replicas_ready_desired=None,
        recurrence_24h=[],
        chronic_count=15,
    )
    assert field is not None
    assert "chronic" in field["value"]
    assert "15" in field["value"]


def test_tldr_oom_killed_pattern():
    field = _build_tldr_field(
        summary="x",
        pod_events=[
            {"reason": "OOMKilled", "count": 2},
            {"reason": "OOMKilled", "count": 1},
        ],
        recent_deploys=None,
        replicas_ready_desired=None,
        recurrence_24h=[],
    )
    assert field is not None
    assert "OOMKilled pattern" in field["value"]


def test_tldr_fallback_summary_truncates_to_80_chars():
    long = "x" * 120
    field = _build_tldr_field(
        summary=long,
        pod_events=None,
        recent_deploys=None,
        replicas_ready_desired=None,
        recurrence_24h=[],
    )
    assert field is not None
    # 80 chars + "…"
    assert len(field["value"]) <= 82


def test_tldr_returns_none_when_no_data():
    assert _build_tldr_field(
        summary=None,
        pod_events=None,
        recent_deploys=None,
        replicas_ready_desired=None,
        recurrence_24h=None,
    ) is None


def test_is_majority_replicas_down_helper():
    assert _is_majority_replicas_down("1/3") is True   # 2 of 3 = 66%
    assert _is_majority_replicas_down("2/3") is False  # 1 of 3 = 33%
    assert _is_majority_replicas_down("0/3") is True
    assert _is_majority_replicas_down("3/3") is False
    assert _is_majority_replicas_down(None) is False
    assert _is_majority_replicas_down("garbage") is False


# ── self-mon footer ─────────────────────────────────────────────────────


def test_self_health_footer_stale_kg_sync_emits_warning_icon():
    text = _self_health_footer(
        "copilot/enrich · groupKey=X",
        self_health_summary={
            "kg_sync_lag_min": 45.0,
            "alerts_resolve_status": "ok",
            "owner_coverage_pct": 86.68,
        },
        build_version="wave-9-uxr",
    )
    assert "KG sync ⚠" in text
    assert "45m ago" in text
    assert "alerts_resolve OK" in text
    assert "owner 86.68%" in text
    assert "build wave-9-uxr" in text


def test_self_health_footer_fresh_kg_no_warning():
    text = _self_health_footer(
        "base",
        self_health_summary={"kg_sync_lag_min": 5.0, "alerts_resolve_status": "ok"},
    )
    assert "⚠" not in text
    assert "KG sync 5m ago" in text


def test_self_health_footer_empty_summary_returns_base():
    text = _self_health_footer("base", self_health_summary=None)
    assert text == "base"


# ── TC build URL ────────────────────────────────────────────────────────


def test_tc_build_url_uses_build_url_when_present():
    url = _tc_build_url(
        build_url="https://wo-teamcity.lastoasisgame.com/viewLog.html?buildId=125133",
        build_id=125133,
        tc_url_prefix="https://other-tc.com",
    )
    assert url == "https://wo-teamcity.lastoasisgame.com/viewLog.html?buildId=125133"


def test_tc_build_url_falls_back_to_build_id():
    url = _tc_build_url(
        build_url=None,
        build_id=125133,
        tc_url_prefix="https://wo-teamcity.lastoasisgame.com",
    )
    assert url == "https://wo-teamcity.lastoasisgame.com/viewLog.html?buildId=125133"


def test_tc_build_url_no_id_no_url_returns_none():
    assert _tc_build_url(build_url=None, build_id=None, tc_url_prefix="https://x.com") is None
    assert _tc_build_url(build_url=None, build_id="", tc_url_prefix="https://x.com") is None


def test_tc_build_url_strips_trailing_slash():
    url = _tc_build_url(
        build_url=None,
        build_id=42,
        tc_url_prefix="https://wo-teamcity.lastoasisgame.com/",
    )
    assert url == "https://wo-teamcity.lastoasisgame.com/viewLog.html?buildId=42"


# ── compact mode + integration ──────────────────────────────────────────


def _make_incident(severity="warning") -> Incident:
    return Incident(
        incident_id="fp-1",
        severity=severity,
        status="firing",
        summary="x",
        description="StatefulSet replicas mismatch.",
        namespace="preprod-shared",
        labels={
            "alertname": "KubeStatefulSetReplicasMismatch",
            "severity": severity,
            "namespace": "preprod-shared",
        },
        annotations={},
        starts_at="2026-05-24T10:00:00Z",
    )


def _make_ctx(severity="warning", **overrides) -> EnrichedContext:
    defaults = dict(
        incident=_make_incident(severity=severity),
        service="clickhouse-keeper",
        team_owner="infra",
        in_kg=True,
    )
    defaults.update(overrides)
    return EnrichedContext(**defaults)


async def _capture_payload(ctx: EnrichedContext, compact_mode: str = "off", resurfaced: bool = False) -> dict:
    sent = {}

    async def fake_post(self, url, json=None, **_):
        if "payload" not in sent:
            sent["payload"] = json
        resp = MagicMock()
        resp.status_code = 200
        resp.text = ""
        resp.json = MagicMock(return_value={"id": "snap-msg-id"})
        return resp

    # Сбрасываем dedup-state чтобы тесты не PATCH-или existing entry.
    from app.services.discord import dedup as dedup_mod
    with dedup_mod._dedup_lock:
        dedup_mod._recent_enriched.clear()

    with patch("app.services.discord_service.settings.DISCORD_DRY_RUN", False), \
         patch("app.services.discord_service.settings.DISCORD_WEBHOOK_URL",
               "https://discord.com/api/webhooks/test/hook"), \
         patch("app.services.discord_service.settings.DISCORD_COMPACT_MODE", compact_mode), \
         patch(
             "app.services.discord.service._collect_self_health_summary",
             return_value=None,
         ), \
         patch("httpx.AsyncClient.post", new=fake_post):
        await DiscordService().send_enriched_alert([ctx], env="preprod", resurfaced=resurfaced)
    return sent["payload"]


@pytest.mark.asyncio
async def test_compact_mode_warning_only_renders_one_line():
    ctx = _make_ctx(severity="warning")
    payload = await _capture_payload(ctx, compact_mode="warning_only")
    assert "embeds" not in payload
    assert "content" in payload
    assert "KubeStatefulSetReplicasMismatch" in payload["content"]
    assert "clickhouse-keeper" in payload["content"]
    assert "@infra" in payload["content"]


@pytest.mark.asyncio
async def test_compact_mode_warning_only_does_not_compact_critical():
    ctx = _make_ctx(severity="critical")
    payload = await _capture_payload(ctx, compact_mode="warning_only")
    # critical stays full
    assert "embeds" in payload
    assert payload["embeds"][0]["fields"]


@pytest.mark.asyncio
async def test_compact_mode_off_renders_full_embed():
    ctx = _make_ctx(severity="warning")
    payload = await _capture_payload(ctx, compact_mode="off")
    assert "embeds" in payload
    assert payload["embeds"][0]["fields"]


@pytest.mark.asyncio
async def test_critical_severity_emits_here_mention():
    ctx = _make_ctx(severity="critical")
    payload = await _capture_payload(ctx)
    assert payload.get("content") == "@here"
    # parse list содержит "everyone" чтобы Discord резолвил @here
    assert payload["allowed_mentions"]["parse"] == ["everyone"]


@pytest.mark.asyncio
async def test_warning_severity_no_mention():
    ctx = _make_ctx(severity="warning")
    payload = await _capture_payload(ctx)
    assert "content" not in payload or not payload.get("content")
    assert payload["allowed_mentions"]["parse"] == []


@pytest.mark.asyncio
async def test_critical_embed_color_is_red():
    ctx = _make_ctx(severity="critical")
    payload = await _capture_payload(ctx)
    assert payload["embeds"][0]["color"] == SEVERITY_COLOR_CRITICAL


@pytest.mark.asyncio
async def test_resurfaced_embed_color_is_orange():
    ctx = _make_ctx(severity="warning")
    payload = await _capture_payload(ctx, resurfaced=True)
    assert payload["embeds"][0]["color"] == SEVERITY_COLOR_RESURFACED


@pytest.mark.asyncio
async def test_embed_contains_tldr_field_first():
    ctx = _make_ctx(severity="warning")
    payload = await _capture_payload(ctx)
    fields = payload["embeds"][0]["fields"]
    # TL;DR должна быть ПЕРВОЙ
    assert fields[0]["name"] == "🎯 TL;DR"


@pytest.mark.asyncio
async def test_embed_contains_runbook_field_for_known_alertname():
    ctx = _make_ctx(severity="warning")
    payload = await _capture_payload(ctx)
    fields = payload["embeds"][0]["fields"]
    runbook = [f for f in fields if f["name"] == "📖 Runbook"]
    assert len(runbook) == 1
    assert "kube-statefulset-replicas-mismatch" in runbook[0]["value"]


@pytest.mark.asyncio
async def test_embed_recent_deploys_render_clickable_via_tc_url_prefix():
    """sub-task: TC build URL clickable when only build_id present."""
    ctx = _make_ctx(
        severity="warning",
        recent_deploys=[{
            "minutes_before_incident": 14,
            "number": 2138,
            "buildtype_name": "Build and update",
            "triggered_by": "wizaryx",
            "build_id": 125133,
            "url": None,  # нет URL в extras → берём через build_id
        }],
    )
    with patch("app.services.discord_service.settings.TC_URL_PREFIX",
               "https://wo-teamcity.lastoasisgame.com"):
        payload = await _capture_payload(ctx, compact_mode="off")
    deploys_field = next(
        f for f in payload["embeds"][0]["fields"] if "Recent deploys" in f["name"]
    )
    assert "viewLog.html?buildId=125133" in deploys_field["value"]
    assert "wizaryx" in deploys_field["value"]
