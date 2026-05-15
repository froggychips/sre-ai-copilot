"""Тесты на app.services.alert_dedup (L2 + L4).

L2 — Redis suppress-on-recurrence:
  - первый fire → SEND
  - 2-й/3-й в окне → SEND до 3-го, на ≥3-м SUPPRESS_CHRONIC
  - после quiet >2h → SEND_RESURFACED + reset counter
  - Redis down → SEND_NO_DEDUP fail-open
  - пустой service → SEND_NO_DEDUP

L4 — rollout-noise <10min silent:
  - mismatch alert + предыдущий fire длился <10m → SUPPRESS_ROLLOUT
  - mismatch alert + предыдущий длился >10m → проходит к L2
  - non-mismatch alertname → L4 не триггерится
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.alert_dedup import (CHRONIC_MIN_COUNT,
                                      CHRONIC_QUIET_RESET_SECONDS,
                                      ROLLOUT_NOISE_THRESHOLD_SECONDS,
                                      Decision, decide_send)


@pytest.fixture
def fake_redis():
    """In-memory mock того же интерфейса что aioredis (get/set/delete)."""
    store: dict[str, str] = {}

    class FakeRedis:
        async def get(self, key):
            return store.get(key)

        async def set(self, key, value, ex=None):
            store[key] = value

        async def delete(self, key):
            store.pop(key, None)

    fake = FakeRedis()
    with patch("app.services.alert_dedup._get_client", return_value=fake):
        yield fake, store


# ── L2: chronic suppress ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_fire_returns_send(fake_redis):
    db = MagicMock()
    d = await decide_send(
        "KubePodCrashLooping", "preprod-kingdom2", "bot-service", "warning", db,
    )
    assert d == Decision.SEND
    # State записан
    _, store = fake_redis
    assert any("bot-service" in k for k in store.keys())


@pytest.mark.asyncio
async def test_chronic_suppress_after_third_fire(fake_redis):
    db = MagicMock()
    # 1st fire
    d1 = await decide_send("KubePodCrashLooping", "ns", "bot-service", "warning", db)
    assert d1 == Decision.SEND
    # 2nd fire через 1 минуту
    fire2 = datetime.now(timezone.utc) + timedelta(minutes=1)
    d2 = await decide_send(
        "KubePodCrashLooping", "ns", "bot-service", "warning", db, fire_at=fire2,
    )
    assert d2 == Decision.SEND
    # 3rd fire через 2 минуты — count=3 → SUPPRESS_CHRONIC
    fire3 = datetime.now(timezone.utc) + timedelta(minutes=2)
    d3 = await decide_send(
        "KubePodCrashLooping", "ns", "bot-service", "warning", db, fire_at=fire3,
    )
    assert d3 == Decision.SUPPRESS_CHRONIC


@pytest.mark.asyncio
async def test_quiet_reset_resurfaced(fake_redis):
    db = MagicMock()
    base = datetime.now(timezone.utc)
    # Записываем фейковый state как-будто было 5 fires час назад,
    # но last_fire >2h назад.
    _, store = fake_redis
    key = "enrich:lastsent:KubePodCrashLooping:bot-service"
    old_last = base - timedelta(hours=3)
    store[key] = json.dumps({
        "first": int((base - timedelta(hours=4)).timestamp()),
        "last": int(old_last.timestamp()),
        "count": 8,
    })
    d = await decide_send(
        "KubePodCrashLooping", "ns", "bot-service", "warning", db, fire_at=base,
    )
    assert d == Decision.SEND_RESURFACED
    # State сброшен: count=1
    new_state = json.loads(store[key])
    assert new_state["count"] == 1


@pytest.mark.asyncio
async def test_redis_down_failopen():
    db = MagicMock()

    class FailingRedis:
        async def get(self, key):
            raise ConnectionError("redis down")

    with patch("app.services.alert_dedup._get_client", return_value=FailingRedis()):
        d = await decide_send(
            "KubePodCrashLooping", "ns", "bot-service", "warning", db,
        )
    assert d == Decision.SEND_NO_DEDUP


@pytest.mark.asyncio
async def test_empty_service_returns_send_no_dedup():
    db = MagicMock()
    d = await decide_send("KubePodCrashLooping", "ns", None, "warning", db)
    assert d == Decision.SEND_NO_DEDUP


# ── L4: rollout-noise silent ────────────────────────────────────────


@pytest.mark.asyncio
async def test_rollout_silent_when_previous_short(fake_redis):
    """Mismatch + предыдущий fire резолвнулся за <10m → SUPPRESS_ROLLOUT."""
    db = MagicMock()
    now = datetime.now(timezone.utc)
    with patch("app.services.alert_dedup.incidents_on") as mock_incidents:
        mock_incidents.return_value = [{
            "alertname": "KubeDeploymentGenerationMismatch",
            "severity": "warning",
            "fingerprint": "x",
            "fired_at": now - timedelta(hours=1),
            "resolved_at": now - timedelta(hours=1) + timedelta(minutes=3),
        }]
        d = await decide_send(
            "KubeDeploymentGenerationMismatch",
            "prod-kingdom5",
            "vm-kube-state-metrics",
            "warning",
            db,
            fire_at=now,
        )
    assert d == Decision.SUPPRESS_ROLLOUT


@pytest.mark.asyncio
async def test_rollout_no_silent_when_previous_long(fake_redis):
    """Mismatch + предыдущий длился >10m → не считаем noise, идём в L2."""
    db = MagicMock()
    now = datetime.now(timezone.utc)
    with patch("app.services.alert_dedup.incidents_on") as mock_incidents:
        mock_incidents.return_value = [{
            "alertname": "KubeDeploymentGenerationMismatch",
            "fingerprint": "x",
            "fired_at": now - timedelta(hours=1),
            "resolved_at": now - timedelta(hours=1) + timedelta(minutes=20),
        }]
        d = await decide_send(
            "KubeDeploymentGenerationMismatch",
            "prod-kingdom5",
            "vm-kube-state-metrics",
            "warning",
            db,
            fire_at=now,
        )
    assert d == Decision.SEND  # L2 first fire


@pytest.mark.asyncio
async def test_rollout_silent_not_triggered_for_crashloop():
    """Non-mismatch alertname → L4 не активируется, идёт L2."""
    db = MagicMock()
    fake = MagicMock()
    fake.get = AsyncMock(return_value=None)
    fake.set = AsyncMock()
    with patch("app.services.alert_dedup._get_client", return_value=fake):
        d = await decide_send(
            "KubePodCrashLooping",
            "preprod-kingdom2",
            "bot-service",
            "warning",
            db,
        )
    assert d == Decision.SEND
