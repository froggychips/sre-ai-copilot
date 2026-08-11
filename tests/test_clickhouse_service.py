"""Тесты на clickhouse_service (blast radius).

Покрывают вторую волну кодревью:
  1. `_parse_ts` понимает startsAt Alertmanager-а с дробными секундами —
     раньше набор strptime-форматов был без %f, реальный алерт давал None,
     и blast radius МОЛЧА выключался.
  2. Схема эндпоинта берётся из порта/настройки, а не хардкодного `http://`
     (в .env.example CH_PROD_PORT=8443 — TLS).
  3. Не-prod namespace НЕ получает прод-цифры активных игроков, подписанные
     своим именем: источник один (CH_PROD_HOST/WOAnalytics), per-env среза
     в нём нет → секции для preprod/preupdate/squad-N просто нет.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.services.clickhouse_service import (
    ClickHouseClient,
    _ch_scheme,
    _ns_to_ch_env,
    _parse_ts,
    get_blast_radius,
)


# ── _parse_ts ──────────────────────────────────────────────────────────────

def test_parse_ts_accepts_milliseconds_with_z():
    """Ровно то, что шлёт Alertmanager: RFC3339 + миллисекунды + Z.

    До фикса → None → blast radius не считался вообще.
    """
    dt = _parse_ts("2026-08-10T12:34:56.789Z")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.replace(microsecond=0) == datetime(2026, 8, 10, 12, 34, 56, tzinfo=timezone.utc)
    assert dt.microsecond == 789000


def test_parse_ts_accepts_nanoseconds_from_go():
    """Go-сериализация умеет 9 знаков дробной части — fromisoformat ест ≤6."""
    dt = _parse_ts("2026-08-10T12:34:56.123456789Z")
    assert dt is not None
    assert dt.microsecond == 123456
    assert dt.tzinfo is not None


def test_parse_ts_accepts_plain_z():
    dt = _parse_ts("2026-08-10T12:34:56Z")
    assert dt == datetime(2026, 8, 10, 12, 34, 56, tzinfo=timezone.utc)


def test_parse_ts_accepts_offset_and_fractional_offset():
    assert _parse_ts("2026-08-10T15:34:56+03:00").astimezone(timezone.utc) == datetime(
        2026, 8, 10, 12, 34, 56, tzinfo=timezone.utc
    )
    dt = _parse_ts("2026-08-10T15:34:56.500+03:00")
    assert dt is not None
    assert dt.astimezone(timezone.utc).replace(microsecond=0) == datetime(
        2026, 8, 10, 12, 34, 56, tzinfo=timezone.utc
    )


def test_parse_ts_naive_treated_as_utc():
    dt = _parse_ts("2026-08-10T12:34:56")
    assert dt is not None
    assert dt.tzinfo is timezone.utc


def test_parse_ts_rejects_garbage():
    assert _parse_ts("") is None
    assert _parse_ts("   ") is None
    assert _parse_ts("not-a-date") is None
    assert _parse_ts("2026/08/10 12:34") is None


# ── схема эндпоинта (порт 8443 = TLS) ──────────────────────────────────────

def test_ch_scheme_auto_uses_https_for_tls_ports(monkeypatch):
    monkeypatch.setattr(settings, "CH_PROD_SCHEME", "auto")
    assert _ch_scheme(8443) == "https"
    assert _ch_scheme(443) == "https"


def test_ch_scheme_auto_uses_http_for_plain_ports(monkeypatch):
    monkeypatch.setattr(settings, "CH_PROD_SCHEME", "auto")
    assert _ch_scheme(8123) == "http"
    assert _ch_scheme(8725) == "http"


def test_ch_scheme_explicit_setting_wins(monkeypatch):
    """Нестандартный TLS-порт закрывается настройкой, без правки кода."""
    monkeypatch.setattr(settings, "CH_PROD_SCHEME", "https")
    assert _ch_scheme(9000) == "https"
    monkeypatch.setattr(settings, "CH_PROD_SCHEME", "http")
    assert _ch_scheme(8443) == "http"


def test_client_base_url_follows_port(monkeypatch):
    monkeypatch.setattr(settings, "CH_PROD_SCHEME", "auto")
    tls = ClickHouseClient("ch.example", 8443, "u", "p")
    plain = ClickHouseClient("ch.example", 8123, "u", "p")
    assert tls._base == "https://ch.example:8443"
    assert plain._base == "http://ch.example:8123"


# ── namespace → env ────────────────────────────────────────────────────────

def test_ns_to_ch_env_maps_game_envs():
    assert _ns_to_ch_env("prod-kingdom4") == "prod"
    assert _ns_to_ch_env("prod") == "prod"
    assert _ns_to_ch_env("preprod-kingdom1") == "preprod"
    assert _ns_to_ch_env("preupdate-shared") == "preupdate"
    assert _ns_to_ch_env("squad-12-shared") == "squad-12"


def test_ns_to_ch_env_returns_none_for_non_game_namespace():
    """Прежний fallback возвращал 'prod' для чего угодно — и monitoring-алерт
    получал прод-цифры игроков как свои."""
    assert _ns_to_ch_env("monitoring") is None
    assert _ns_to_ch_env("kube-system") is None
    assert _ns_to_ch_env("sre-ai") is None


# ── get_blast_radius: только prod ──────────────────────────────────────────

_ROWS = [
    {"Minute": "2026-08-10 12:20:00", "active_users": 1000},
    {"Minute": "2026-08-10 12:21:00", "active_users": 1000},
    {"Minute": "2026-08-10 12:30:00", "active_users": 100},
    {"Minute": "2026-08-10 12:31:00", "active_users": 100},
]


@pytest.fixture()
def ch_configured(monkeypatch):
    monkeypatch.setattr(settings, "CH_PROD_HOST", "ch.example")
    monkeypatch.setattr(settings, "CH_PROD_PORT", 8443)
    monkeypatch.setattr(settings, "CH_PROD_USER", "reader")
    monkeypatch.setattr(settings, "CH_PROD_PASSWORD", "secret")
    monkeypatch.setattr(settings, "CH_BLAST_RADIUS_WINDOW_MINUTES", 15)


@pytest.mark.asyncio
async def test_blast_radius_prod_namespace_returns_section(ch_configured):
    with patch.object(ClickHouseClient, "query", new=AsyncMock(return_value=_ROWS)) as q:
        out = await get_blast_radius("prod-kingdom4", "2026-08-10T12:30:00.512Z")
    assert out is not None
    assert out.startswith("=== BLAST RADIUS (prod env) ===")
    assert "Player activity drop" in out
    assert q.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "namespace",
    ["preprod-kingdom1", "preupdate-shared", "squad-12-shared", "squad-gd-shared", "monitoring"],
)
async def test_blast_radius_non_prod_namespace_gets_no_prod_numbers(ch_configured, namespace):
    """Главная находка: инцидент в не-prod окружении не должен получать
    прод-цифры активных игроков, подписанные ЕГО именем.

    Проверяем и результат (None → секции нет), и что запрос к прод-CH вообще
    не уходил.
    """
    with patch.object(ClickHouseClient, "query", new=AsyncMock(return_value=_ROWS)) as q:
        out = await get_blast_radius(namespace, "2026-08-10T12:30:00Z")
    assert out is None
    assert q.await_count == 0


@pytest.mark.asyncio
async def test_blast_radius_never_labels_data_with_foreign_env(ch_configured):
    """Ни один вывод не должен содержать чужой env в заголовке."""
    with patch.object(ClickHouseClient, "query", new=AsyncMock(return_value=_ROWS)):
        for ns in ("prod-kingdom4", "preprod-kingdom1", "squad-7-shared"):
            out = await get_blast_radius(ns, "2026-08-10T12:30:00Z")
            if out is not None:
                assert "(prod env)" in out


@pytest.mark.asyncio
async def test_blast_radius_disabled_without_credentials(monkeypatch):
    monkeypatch.setattr(settings, "CH_PROD_HOST", "")
    monkeypatch.setattr(settings, "CH_PROD_PASSWORD", "")
    with patch.object(ClickHouseClient, "query", new=AsyncMock(return_value=_ROWS)) as q:
        assert await get_blast_radius("prod-kingdom4", "2026-08-10T12:30:00Z") is None
    assert q.await_count == 0


@pytest.mark.asyncio
async def test_blast_radius_unparsable_ts_returns_none(ch_configured):
    with patch.object(ClickHouseClient, "query", new=AsyncMock(return_value=_ROWS)) as q:
        assert await get_blast_radius("prod-kingdom4", "10.08.2026 12:30") is None
    assert q.await_count == 0
