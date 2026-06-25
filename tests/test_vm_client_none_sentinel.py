"""Регрессия diag H1: query_instant возвращает None при «нет данных», НЕ 0.0.

Контракт: частичный сбой VictoriaMetrics (один из 13 запросов
get_cluster_health таймаутит/пуст) не должен давать ложно-нулевой и потому
«здоровый» снимок кластера, который уходит в LLM. None («нет данных») должен
быть отличим от настоящего нуля (метрика есть, значение 0).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.context.vm_client import ClusterHealth, VMClient


def _patch_async_client(mock_resp: MagicMock):
    """Контекст-менеджер-патч httpx.AsyncClient под один GET-ответ."""
    cm = patch("httpx.AsyncClient")
    mock_client_cls = cm.start()
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client_cls.return_value = mock_client
    return cm


def _resp(payload: dict) -> MagicMock:
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status = MagicMock()
    return r


# ── query_instant контракт ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_instant_real_zero_is_zero_not_none():
    """Настоящий ноль (VM отдала "0") → 0.0, НЕ None: данные есть, значение 0."""
    resp = _resp({"data": {"result": [{"value": [123, "0"]}]}})
    cm = _patch_async_client(resp)
    try:
        vm = VMClient("http://vm:8428")
        val = await vm.query_instant("sum(pods_failed)")
    finally:
        cm.stop()
    assert val == 0.0
    assert val is not None


@pytest.mark.asyncio
async def test_query_instant_real_value():
    resp = _resp({"data": {"result": [{"value": [123, "42.5"]}]}})
    cm = _patch_async_client(resp)
    try:
        vm = VMClient("http://vm:8428")
        val = await vm.query_instant("q")
    finally:
        cm.stop()
    assert val == 42.5


@pytest.mark.asyncio
async def test_query_instant_empty_result_is_none():
    """Пустой ответ VM = «нет данных» → None (раньше было 0.0)."""
    resp = _resp({"data": {"result": []}})
    cm = _patch_async_client(resp)
    try:
        vm = VMClient("http://vm:8428")
        val = await vm.query_instant("q")
    finally:
        cm.stop()
    assert val is None


@pytest.mark.asyncio
async def test_query_instant_nan_is_none():
    resp = _resp({"data": {"result": [{"value": [123, "NaN"]}]}})
    cm = _patch_async_client(resp)
    try:
        vm = VMClient("http://vm:8428")
        val = await vm.query_instant("q")
    finally:
        cm.stop()
    assert val is None


@pytest.mark.asyncio
async def test_query_instant_http_error_is_none():
    """Сетевой/HTTP-сбой → None (нечего ретраить, но и не врём нулём)."""
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=RuntimeError("connection refused"))
        mock_client_cls.return_value = mock_client
        vm = VMClient("http://vm:8428")
        val = await vm.query_instant("q")
    assert val is None


# ── get_cluster_health: частичный сбой не выглядит здоровым ──────────────────

# Полный «здоровый» набор: все 13 метрик присутствуют, нулевые проблемы.
_HEALTHY = {
    "nodes_total": 16, "nodes_ready": 16,
    "pods_running": 400, "pods_pending": 0, "pods_failed": 0,
    "crashloops": 0, "deploy_mismatch": 0,
    "cpu_pct": 30.0, "mem_pct": 50.0, "disk_peak_pct": 40.0,
    "alerts_critical": 0, "alerts_warning": 0, "alerts_prod": 0,
}


@pytest.mark.asyncio
async def test_cluster_health_happy_path_unchanged():
    """Все метрики есть, проблем нет → healthy, data_available=True."""
    vm = VMClient("http://vm:8428")

    # get_cluster_health строит фиксированный dict в порядке вставки queries,
    # который совпадает с порядком ключей _HEALTHY — отдаём значения по позиции.
    seq = iter(_HEALTHY.values())

    async def positional(self, query):  # noqa: ANN001
        return next(seq)

    with patch.object(VMClient, "query_instant", positional):
        health = await vm.get_cluster_health()

    assert health.data_available is True
    assert health.health_status == "healthy"
    assert health.pods_failed == 0
    assert health.nodes_total == 16


@pytest.mark.asyncio
async def test_cluster_health_partial_failure_not_healthy():
    """Один ключевой запрос (pods_failed) None → снимок НЕ healthy/полный.

    Раньше None→0 давал ложно-«здоровый» снимок. Теперь pods_failed в наборе
    _CORE_KEYS → data_available=False, health_status=unknown.
    """
    vm = VMClient("http://vm:8428")

    partial = dict(_HEALTHY)
    seq = iter(partial.values())
    keys = list(partial.keys())
    fail_idx = keys.index("pods_failed")

    async def positional(self, query, _counter=[0]):  # noqa: ANN001,B006
        i = _counter[0]
        _counter[0] += 1
        # эмулируем таймаут pods_failed-запроса → None
        return None if i == fail_idx else list(partial.values())[i]

    with patch.object(VMClient, "query_instant", positional):
        health = await vm.get_cluster_health()

    # метрика помечена как «нет данных», не 0
    assert health.to_dict()["pods_failed"] is None
    assert health.data_available is False
    assert health.health_status == "unknown"


@pytest.mark.asyncio
async def test_cluster_health_signal_metric_missing_not_healthy():
    """Не-core сигнальная метрика (alerts_prod) None → не «healthy».

    Ядро здорово, но alerts_prod не доехала: раньше None→0 прятал возможную
    prod-тревогу как «здорово». Теперь снимок degraded, не healthy.
    """
    vm = VMClient("http://vm:8428")
    partial = dict(_HEALTHY)
    keys = list(partial.keys())
    drop_idx = keys.index("alerts_prod")

    async def positional(self, query, _counter=[0]):  # noqa: ANN001,B006
        i = _counter[0]
        _counter[0] += 1
        return None if i == drop_idx else list(partial.values())[i]

    with patch.object(VMClient, "query_instant", positional):
        health = await vm.get_cluster_health()

    assert health.to_dict()["alerts_prod"] is None
    # ядро цело → данные доступны, но не врём «healthy»
    assert health.data_available is True
    assert health.health_status == "degraded"


@pytest.mark.asyncio
async def test_cluster_health_total_outage_unknown():
    """VM полностью недоступна (все запросы кидают) → unknown, не healthy."""
    vm = VMClient("http://vm:8428")

    async def boom(self, query):  # noqa: ANN001
        raise RuntimeError("VM down")

    with patch.object(VMClient, "query_instant", boom):
        health = await vm.get_cluster_health()

    assert health.data_available is False
    assert health.health_status == "unknown"


# ── ClusterHealth: настоящий ноль ≠ отсутствие данных ────────────────────────


def test_clusterhealth_real_zero_is_healthy():
    """Все метрики присутствуют и нулевые (нет проблем) → healthy."""
    ch = ClusterHealth(dict(_HEALTHY))
    assert ch.data_available is True
    assert ch.health_status == "healthy"
    assert ch.pods_failed == 0


def test_clusterhealth_none_core_is_unknown():
    m = dict(_HEALTHY)
    m["nodes_total"] = None
    ch = ClusterHealth(m)
    assert ch.data_available is False
    assert ch.health_status == "unknown"


def test_clusterhealth_real_problem_still_detected():
    """Регресс-страховка: настоящий pods_failed>0 даёт degraded."""
    m = dict(_HEALTHY)
    m["pods_failed"] = 3
    ch = ClusterHealth(m)
    assert ch.data_available is True
    assert ch.health_status == "degraded"
