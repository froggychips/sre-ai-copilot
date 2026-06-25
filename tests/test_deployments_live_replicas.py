"""Тесты для fetch_live_replicas — дедлайн пер-вызов, без глобального сокета.

Регрессия (P1, concurrency-hazard): раньше дедлайн ставился через
socket.setdefaulttimeout — процесс-глобально. Конкурентные вызовы
(Celery + asyncio.gather) рейсились, finally восстанавливал чужой timeout
и ронял таймауты у всех остальных сокетов процесса (httpx/DB/redis/k8s).

Фикс: timeout передаётся в сам вызов k8s-API через `_request_timeout`.
Глобальный socket.getdefaulttimeout() обязан остаться неизменным.
"""
import socket
from unittest.mock import MagicMock, patch

from app.context import deployments


def _sts(ready, desired):
    obj = MagicMock()
    obj.spec.replicas = desired
    obj.status.ready_replicas = ready
    return obj


def test_fetch_live_replicas_passes_request_timeout():
    """timeout прокидывается в k8s-вызов как `_request_timeout`, не в socket."""
    api = MagicMock()
    api.read_namespaced_stateful_set.return_value = _sts(1, 3)
    with patch.object(deployments, "_load_k8s_once", return_value=True), \
            patch.object(deployments.client, "AppsV1Api", return_value=api):
        res = deployments.fetch_live_replicas(
            "ns-x", "sts-x", kind_hint="statefulset", timeout_sec=2.5
        )
    assert res == {"ready": 1, "desired": 3}
    api.read_namespaced_stateful_set.assert_called_once_with(
        "sts-x", "ns-x", _request_timeout=2.5
    )


def test_fetch_live_replicas_does_not_touch_global_socket_timeout():
    """Главная регрессия: глобальный socket-таймаут не меняется вызовом."""
    api = MagicMock()
    api.read_namespaced_deployment.return_value = _sts(2, 2)

    sentinel = 17.0
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(sentinel)
    try:
        before = socket.getdefaulttimeout()
        with patch.object(deployments, "_load_k8s_once", return_value=True), \
                patch.object(deployments.client, "AppsV1Api", return_value=api):
            deployments.fetch_live_replicas(
                "ns-y", "dep-y", kind_hint="deployment", timeout_sec=0.5
            )
        after = socket.getdefaulttimeout()
        assert before == after == sentinel, (
            "fetch_live_replicas изменил процесс-глобальный socket-таймаут"
        )
    finally:
        socket.setdefaulttimeout(old)


def test_fetch_live_replicas_global_socket_unchanged_on_error():
    """Даже при ошибке k8s-вызова глобальный socket-таймаут не трогается."""
    api = MagicMock()
    api.read_namespaced_stateful_set.side_effect = RuntimeError("boom")
    api.read_namespaced_deployment.side_effect = RuntimeError("boom")

    before = socket.getdefaulttimeout()
    with patch.object(deployments, "_load_k8s_once", return_value=True), \
            patch.object(deployments.client, "AppsV1Api", return_value=api):
        res = deployments.fetch_live_replicas("ns", "name", timeout_sec=1.0)
    assert res is None
    assert socket.getdefaulttimeout() == before


def test_module_no_longer_calls_socket_default_timeout_api():
    """Guard: socket.set/getdefaulttimeout не ВЫЗЫВАЕТСЯ в модуле.

    Проверяем AST, а не сырой текст — чтобы упоминания в комментариях
    (объясняющие, почему так делать нельзя) не ломали тест.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(deployments))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
    }
    assert "setdefaulttimeout" not in called
    assert "getdefaulttimeout" not in called
