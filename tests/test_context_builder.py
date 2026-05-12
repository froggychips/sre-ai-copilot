"""Unit-тесты для ContextBuilder.

Цель: проверить async-сборку контекста и fallback при отсутствии k8s API
(чтобы CI-runner / dev-машина без kubeconfig могли импортировать модуль).
"""

from unittest.mock import patch

import pytest
from kubernetes.config.config_exception import ConfigException


@pytest.fixture
def builder_no_k8s():
    """Имитируем dev-машину без k8s: и in-cluster, и kubeconfig фейлят."""
    with patch(
        "app.context.context_builder.config.load_incluster_config",
        side_effect=ConfigException("no in-cluster"),
    ), patch(
        "app.context.context_builder.config.load_kube_config",
        side_effect=ConfigException("no kubeconfig"),
    ):
        from app.context.context_builder import ContextBuilder
        yield ContextBuilder()


@pytest.fixture
def builder_with_k8s():
    """Имитируем pod в кластере: in-cluster config доступен."""
    with patch(
        "app.context.context_builder.config.load_incluster_config",
        return_value=None,
    ):
        from app.context.context_builder import ContextBuilder
        yield ContextBuilder()


@pytest.mark.asyncio
async def test_no_k8s_fallback(builder_no_k8s):
    """Без k8s API build_context отдаёт stub, не падает."""
    assert builder_no_k8s.k8s_available is False
    ctx = await builder_no_k8s.build_context({"targets": [{"namespace": "squad-1"}]})
    assert ctx["incident"] == {"targets": [{"namespace": "squad-1"}]}
    assert ctx["metrics"] is None
    assert ctx["deployments"] == []
    assert ctx["logs_summary"] == "k8s api unavailable"


@pytest.mark.asyncio
async def test_kubeconfig_fallback_when_incluster_fails():
    """Если in-cluster не сработал, должен подняться local kubeconfig."""
    with patch(
        "app.context.context_builder.config.load_incluster_config",
        side_effect=ConfigException("not in cluster"),
    ) as incluster, patch(
        "app.context.context_builder.config.load_kube_config",
        return_value=None,
    ) as local:
        from app.context.context_builder import ContextBuilder
        cb = ContextBuilder()
        assert cb.k8s_available is True
        incluster.assert_called_once()
        local.assert_called_once()


@pytest.mark.asyncio
async def test_build_context_collects_all_sources(builder_with_k8s):
    """build_context дёргает metrics/deps/logs в threadpool и собирает в dict."""
    with patch.object(
        builder_with_k8s.metrics, "get_namespace_health", return_value={"cpu": "ok"}
    ), patch.object(
        builder_with_k8s.deps, "get_recent_deployments", return_value=[{"name": "api"}]
    ), patch.object(
        builder_with_k8s.logs, "get_summary", return_value="200 lines OK"
    ):
        ctx = await builder_with_k8s.build_context(
            {"targets": [{"namespace": "squad-1", "pod": "api-7"}]}
        )

    assert ctx["metrics"] == {"cpu": "ok"}
    assert ctx["deployments"] == [{"name": "api"}]
    assert ctx["logs_summary"] == "200 lines OK"


@pytest.mark.asyncio
async def test_build_context_skips_logs_when_no_pod(builder_with_k8s):
    """Без pod в targets — логи не собираются (placeholder)."""
    with patch.object(
        builder_with_k8s.metrics, "get_namespace_health", return_value={}
    ), patch.object(
        builder_with_k8s.deps, "get_recent_deployments", return_value=[]
    ), patch.object(
        builder_with_k8s.logs, "get_summary", return_value="should-not-be-called"
    ) as logs_mock:
        ctx = await builder_with_k8s.build_context(
            {"targets": [{"namespace": "squad-1"}]}
        )
    logs_mock.assert_not_called()
    assert ctx["logs_summary"] == "No pod target"


@pytest.mark.asyncio
async def test_build_context_default_namespace(builder_with_k8s):
    """Если targets пуст — namespace=default."""
    captured = {}

    def fake_metrics(ns):
        captured["ns"] = ns
        return None

    with patch.object(
        builder_with_k8s.metrics, "get_namespace_health", side_effect=fake_metrics
    ), patch.object(
        builder_with_k8s.deps, "get_recent_deployments", return_value=[]
    ):
        await builder_with_k8s.build_context({})
    assert captured["ns"] == "default"
