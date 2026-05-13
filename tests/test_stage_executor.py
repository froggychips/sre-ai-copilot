"""Тесты на IncidentPipeline.stage_executor — PR #2 executor track.

Что проверяем:
  - EXECUTOR_ENABLED=False → стадия пропускается, status=skipped/executor_disabled.
  - execution_intent=None → status=skipped/no_intent.
  - happy path: K8sService.execute_intent(intent, dry_run=True) → dry_run_ok.
  - guardrail block: K8sService возвращает GUARDRAIL_BLOCK → status=guardrail_blocked.
  - exception в K8sService → status=error, пайплайн не падает (advisory-fallback).
"""
from unittest.mock import MagicMock, patch

import pytest

from app.core.execution_dsl import ActionType, ExecutionIntent
from app.workers.pipeline import IncidentPipeline


def _build_pipeline(intent=None) -> IncidentPipeline:
    """Минимальный pipeline для прогона стадии без полного init-а."""
    pl = IncidentPipeline.__new__(IncidentPipeline)
    pl.incident_id = "smoke-1"
    pl.execution_intent = intent
    pl.executor_result = None
    pl.traces = []
    pl.root_span = MagicMock()
    return pl


def _valid_intent() -> ExecutionIntent:
    return ExecutionIntent(
        action=ActionType.RESTART_DEPLOYMENT,
        resource_type="deployment",
        resource_name="town-service",
        namespace="squad-1",
        params={},
        risk="low",
    )


@pytest.mark.asyncio
async def test_executor_skipped_when_disabled():
    pl = _build_pipeline(intent=_valid_intent())
    with patch("app.workers.pipeline.settings") as mock_settings:
        mock_settings.EXECUTOR_ENABLED = False
        await pl.stage_executor()
    assert pl.executor_result == {"status": "skipped", "reason": "executor_disabled"}


@pytest.mark.asyncio
async def test_executor_skipped_when_no_intent():
    pl = _build_pipeline(intent=None)
    with patch("app.workers.pipeline.settings") as mock_settings:
        mock_settings.EXECUTOR_ENABLED = True
        await pl.stage_executor()
    assert pl.executor_result == {"status": "skipped", "reason": "no_intent"}


@pytest.mark.asyncio
async def test_executor_dry_run_ok_path():
    pl = _build_pipeline(intent=_valid_intent())
    fake_k8s = MagicMock()
    fake_k8s.execute_intent.return_value = {
        "success": True,
        "stdout": "deployment.apps/town-service restarted (dry run)",
        "stderr": "",
        "command": "kubectl rollout restart deployment/town-service -n squad-1 --dry-run=server",
    }
    with patch("app.workers.pipeline.settings") as mock_settings, \
         patch.dict("sys.modules", {"app.services.k8s_service": MagicMock(k8s_service=fake_k8s)}):
        mock_settings.EXECUTOR_ENABLED = True
        await pl.stage_executor()
    assert pl.executor_result is not None
    assert pl.executor_result["status"] == "dry_run_ok"
    assert "town-service restarted" in pl.executor_result["stdout"]
    fake_k8s.execute_intent.assert_called_once()


@pytest.mark.asyncio
async def test_executor_captures_guardrail_block():
    pl = _build_pipeline(intent=_valid_intent())
    fake_k8s = MagicMock()
    fake_k8s.execute_intent.return_value = {
        "success": False,
        "error": "GUARDRAIL_BLOCK: write_outside_squad",
    }
    with patch("app.workers.pipeline.settings") as mock_settings, \
         patch.dict("sys.modules", {"app.services.k8s_service": MagicMock(k8s_service=fake_k8s)}):
        mock_settings.EXECUTOR_ENABLED = True
        await pl.stage_executor()
    assert pl.executor_result is not None
    assert pl.executor_result["status"] == "guardrail_blocked"
    assert "write_outside_squad" in pl.executor_result["reason"]


@pytest.mark.asyncio
async def test_executor_catches_exception_advisory_fallback():
    pl = _build_pipeline(intent=_valid_intent())
    fake_k8s = MagicMock()
    fake_k8s.execute_intent.side_effect = RuntimeError("kubectl binary missing")
    with patch("app.workers.pipeline.settings") as mock_settings, \
         patch.dict("sys.modules", {"app.services.k8s_service": MagicMock(k8s_service=fake_k8s)}):
        mock_settings.EXECUTOR_ENABLED = True
        await pl.stage_executor()  # не должно бросить
    assert pl.executor_result is not None
    assert pl.executor_result["status"] == "error"
    assert pl.executor_result["error_type"] == "RuntimeError"
    assert "kubectl binary missing" in pl.executor_result["error"]
