"""Регресс: FastAPI lifespan-shutdown НЕ должен дёргать celery_app.control.shutdown().

Это broker-wide broadcast через Redis: один shutdown-call в api-pod-е шатдаунит
все worker-pods в кластере. На проде это привело к worker-restart циклу при
каждом rolling restart api после deploy-а v0.7.0.

Worker-ы реагируют на SIGTERM от k8s самостоятельно (Celery умеет graceful
shutdown по сигналу). API-процесс не должен ими управлять.
"""
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_lifespan_shutdown_does_not_broadcast_celery_shutdown():
    """При выходе из lifespan-контекста celery_app.control.shutdown() не вызывается."""
    from app.main import lifespan

    # Мокаем зависимости shutdown-а чтобы тест не тянул реальный Redis/DB.
    with patch("app.main.celery_app") as mock_celery, \
         patch("app.main.rate_limit") as mock_rate_limit, \
         patch("app.main.engine") as mock_engine, \
         patch("app.main.start_http_server"):
        # rate_limit.close — async, mock-у нужно поддержать await
        async def _async_noop():
            return None
        mock_rate_limit.close.side_effect = _async_noop

        async with lifespan(None):  # type: ignore[arg-type]
            pass

        # Главное assertion: control.shutdown НЕ вызывался — он broker-wide.
        mock_celery.control.shutdown.assert_not_called()
        # Локальные ресурсы закрылись.
        mock_rate_limit.close.assert_called_once()
        mock_engine.dispose.assert_called_once()
