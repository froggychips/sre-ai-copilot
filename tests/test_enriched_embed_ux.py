"""UX-доработка enriched embed.

Проверяем:
  * human-time forматтер (5+ кейсов: 0, минуты, часы, дни, недели, None)
  * embed-builder: pod_name → field появляется
  * embed-builder: без pod_name → field отсутствует (skip-if-empty)
  * embed-builder: ready/desired формат `1/3`
  * embed-builder: container_reason формат
  * enrich_alert: новые KG-queries вызываются (latest_pod_event_for,
    current_replicas_from_kg) и попадают в EnrichedContext
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models.incident import Incident
from app.services.alert_enrichment import EnrichedContext, enrich_alert
from app.services.discord_service import DiscordService
from app.utils.time_human import humanize_minutes_ago, humanize_seconds_ago


# ── human-time formatter ─────────────────────────────────────────────


def test_humanize_minutes_zero_or_negative_just_now():
    assert humanize_minutes_ago(0) == "just now"
    assert humanize_minutes_ago(-5) == "just now"


def test_humanize_minutes_under_hour_returns_minutes():
    assert humanize_minutes_ago(1) == "1 min ago"
    assert humanize_minutes_ago(42) == "42 min ago"
    assert humanize_minutes_ago(59) == "59 min ago"


def test_humanize_minutes_hours_with_one_decimal():
    # 60 min = 1.0h
    assert humanize_minutes_ago(60) == "1.0h ago"
    # 2778 min (тот самый кейс из feedback) = 46.3h, но 46.3 > 24 → дни.
    # Проверим граничные значения.
    assert humanize_minutes_ago(90) == "1.5h ago"
    # 23h 59m ≈ 23.98h
    assert humanize_minutes_ago(23 * 60 + 59) == "24.0h ago"  # rounds to 24.0


def test_humanize_minutes_days_under_week_decimal():
    # 2778 min = 46.3h = 1.93d → "1.9d ago"
    assert humanize_minutes_ago(2778) == "1.9d ago"
    # 24h boundary
    assert humanize_minutes_ago(24 * 60) == "1.0d ago"
    # 6 days
    assert humanize_minutes_ago(6 * 24 * 60) == "6.0d ago"


def test_humanize_minutes_days_over_week_integer():
    # 7 days exactly — integer
    assert humanize_minutes_ago(7 * 24 * 60) == "7d ago"
    # 12 days
    assert humanize_minutes_ago(12 * 24 * 60) == "12d ago"


def test_humanize_minutes_weeks():
    # 30 days = 4w
    assert humanize_minutes_ago(30 * 24 * 60) == "4w ago"
    # 5 weeks
    assert humanize_minutes_ago(5 * 7 * 24 * 60) == "5w ago"


def test_humanize_minutes_none_returns_placeholder():
    assert humanize_minutes_ago(None) == "?"
    assert humanize_minutes_ago("not-a-number") == "?"


def test_humanize_seconds_under_minute():
    assert humanize_seconds_ago(0) == "just now"
    assert humanize_seconds_ago(5) == "5s ago"
    assert humanize_seconds_ago(59) == "59s ago"
    # 60s → 1 min ago
    assert humanize_seconds_ago(60) == "1 min ago"


# ── builder: pod_name / replicas / reason fields ─────────────────────


def _make_incident() -> Incident:
    return Incident(
        incident_id="fp-1",
        severity="warning",
        status="firing",
        summary="x",
        description="StatefulSet replicas mismatch.",
        namespace="preprod-shared",
        labels={
            "alertname": "KubeStatefulSetReplicasMismatch",
            "severity": "warning",
            "namespace": "preprod-shared",
            "statefulset": "clickhouse-keeper",
        },
        annotations={},
        starts_at="2026-05-24T10:00:00Z",
    )


def _make_ctx(**overrides) -> EnrichedContext:
    defaults = dict(
        incident=_make_incident(),
        service="clickhouse-keeper",
        team_owner="infra",
        in_kg=True,
    )
    defaults.update(overrides)
    return EnrichedContext(**defaults)


async def _capture_embed(ctx: EnrichedContext) -> dict:
    sent = {}

    async def fake_post(self, url, json=None, **_):
        sent["payload"] = json
        resp = MagicMock()
        resp.status_code = 204
        return resp

    with patch("app.services.discord_service.settings.DISCORD_DRY_RUN", False), \
         patch("app.services.discord_service.settings.DISCORD_WEBHOOK_URL",
               "https://example.com/wh"), \
         patch("httpx.AsyncClient.post", new=fake_post):
        await DiscordService().send_enriched_alert([ctx], env="preprod")
    return sent["payload"]["embeds"][0]


@pytest.mark.asyncio
async def test_embed_renders_pod_name_field_when_present():
    ctx = _make_ctx(pod_name="clickhouse-keeper-0")
    embed = await _capture_embed(ctx)
    pod_fields = [f for f in embed["fields"] if f["name"] == "Pod"]
    assert len(pod_fields) == 1
    assert "clickhouse-keeper-0" in pod_fields[0]["value"]


@pytest.mark.asyncio
async def test_embed_skips_pod_name_field_when_absent():
    ctx = _make_ctx()  # no pod_name
    embed = await _capture_embed(ctx)
    pod_fields = [f for f in embed["fields"] if f["name"] == "Pod"]
    assert pod_fields == []


@pytest.mark.asyncio
async def test_embed_renders_replicas_ready_desired():
    ctx = _make_ctx(replicas_ready_desired="1/3")
    embed = await _capture_embed(ctx)
    rep_fields = [f for f in embed["fields"] if f["name"] == "Replicas"]
    assert len(rep_fields) == 1
    assert "1/3" in rep_fields[0]["value"]


@pytest.mark.asyncio
async def test_embed_renders_container_reason():
    ctx = _make_ctx(container_reason="BackOff")
    embed = await _capture_embed(ctx)
    reason_fields = [f for f in embed["fields"] if f["name"] == "Reason"]
    assert len(reason_fields) == 1
    assert "BackOff" in reason_fields[0]["value"]


@pytest.mark.asyncio
async def test_embed_skips_replicas_field_when_absent():
    ctx = _make_ctx()
    embed = await _capture_embed(ctx)
    assert not any(f["name"] == "Replicas" for f in embed["fields"])
    assert not any(f["name"] == "Reason" for f in embed["fields"])


@pytest.mark.asyncio
async def test_embed_pod_events_use_human_time():
    """`2778 мин` raw value → отрендериться как `1.9d ago`, не `2778 мин назад`."""
    ctx = _make_ctx(
        incident=Incident(
            incident_id="fp-2",
            severity="critical",
            status="firing",
            summary="x",
            description="Crash.",
            namespace="preprod-shared",
            labels={"alertname": "KubePodCrashLooping",
                    "severity": "critical",
                    "namespace": "preprod-shared",
                    "pod": "clickhouse-keeper-0"},
            annotations={},
            starts_at="2026-05-24T10:00:00Z",
        ),
        pod_events=[{
            "reason": "BackOff",
            "pod_name": "clickhouse-keeper-0",
            "count": 13203,
            "minutes_before": 2778,
            "message": "Back-off restarting failed container",
        }],
    )
    embed = await _capture_embed(ctx)
    pe_field = next(f for f in embed["fields"] if "Recent pod events" in f["name"])
    # Должно быть human-time, не «2778 мин назад»
    assert "1.9d ago" in pe_field["value"]
    assert "2778 мин назад" not in pe_field["value"]


# ── enrich_alert: новые KG-helpers вызываются ────────────────────────


@patch("app.services.alert_enrichment.current_replicas_from_kg")
@patch("app.services.alert_enrichment.latest_pod_event_for")
@patch("app.services.alert_enrichment.recent_pod_events_for")
@patch("app.services.alert_enrichment.recent_deploys_for")
@patch("app.services.alert_enrichment.nearby_alerts")
@patch("app.services.alert_enrichment.incidents_on")
@patch("app.services.alert_enrichment._downstream_count_by_kind")
def test_enrich_alert_populates_pod_and_replicas_from_kg(
    mock_downstream, mock_incidents, mock_nearby, mock_recent,
    mock_pod_events, mock_latest_event, mock_replicas,
):
    inc = _make_incident()
    mock_recent.return_value = []
    mock_nearby.return_value = []
    mock_incidents.return_value = []
    mock_downstream.return_value = {}
    mock_pod_events.return_value = [{
        "reason": "BackOff",
        "pod_name": "clickhouse-keeper-0",
        "first_seen": datetime(2026, 5, 24, 8, 0, tzinfo=timezone.utc),
        "last_seen": datetime(2026, 5, 24, 10, 0, tzinfo=timezone.utc),
        "count": 13203,
        "minutes_before": 60,
        "message": "Back-off",
    }]
    mock_latest_event.return_value = None  # не нужен — pod_events не пустой
    mock_replicas.return_value = {"ready": 1, "desired": 3}

    db = MagicMock()
    svc_row = MagicMock()
    svc_row.team_owner = "infra"
    svc_row.synthetic = False
    svc_row.updated_at = datetime(2026, 5, 24, 9, 0, tzinfo=timezone.utc)
    db.query.return_value.filter.return_value.one_or_none.return_value = svc_row
    db.query.return_value.filter.return_value.filter.return_value.first.return_value = svc_row

    ctx = enrich_alert(db, inc)

    # KG-queries вызвались
    mock_replicas.assert_called_once()
    # pod_name из head(pod_events) — latest_pod_event_for даже не нужен
    assert ctx.pod_name == "clickhouse-keeper-0"
    assert ctx.container_reason == "BackOff"
    assert ctx.replicas_ready_desired == "1/3"


@patch("app.context.deployments.fetch_live_replicas")
@patch("app.services.alert_enrichment.current_replicas_from_kg")
@patch("app.services.alert_enrichment.latest_pod_event_for")
@patch("app.services.alert_enrichment.recent_pod_events_for")
@patch("app.services.alert_enrichment.recent_deploys_for")
@patch("app.services.alert_enrichment.nearby_alerts")
@patch("app.services.alert_enrichment.incidents_on")
@patch("app.services.alert_enrichment._downstream_count_by_kind")
def test_enrich_alert_falls_back_to_live_k8s_when_kg_empty(
    mock_downstream, mock_incidents, mock_nearby, mock_recent,
    mock_pod_events, mock_latest_event, mock_replicas, mock_live,
):
    inc = _make_incident()
    mock_recent.return_value = []
    mock_nearby.return_value = []
    mock_incidents.return_value = []
    mock_downstream.return_value = {}
    mock_pod_events.return_value = []
    mock_latest_event.return_value = None
    mock_replicas.return_value = None  # KG empty
    mock_live.return_value = {"ready": 0, "desired": 3}

    db = MagicMock()
    svc_row = MagicMock()
    svc_row.team_owner = "infra"
    svc_row.synthetic = False
    svc_row.updated_at = datetime(2026, 5, 24, 9, 0, tzinfo=timezone.utc)
    db.query.return_value.filter.return_value.one_or_none.return_value = svc_row
    db.query.return_value.filter.return_value.filter.return_value.first.return_value = svc_row

    ctx = enrich_alert(db, inc)

    # Live fallback сработал
    mock_live.assert_called_once()
    # kind_hint="statefulset" — берётся из labels.statefulset
    call_kwargs = mock_live.call_args.kwargs
    assert call_kwargs.get("kind_hint") == "statefulset"
    assert ctx.replicas_ready_desired == "0/3"
