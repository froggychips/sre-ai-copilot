"""Hard-gate `LLM_PIPELINE_ENABLED`: защита от случайного LLM-burn.

Сценарий который этот gate закрывает:
  Кто-то меняет VMAlertmanagerConfig URL с /webhooks/alertmanager/store
  на /webhooks/alertmanager. AM начинает слать 50 alerts/мин в full
  pipeline → 5 LLM-calls × $0.05 × 50 ≈ $750/час до того как заметят.

Default `LLM_PIPELINE_ENABLED=False` обрывает task с reason BEFORE
любого LLM-вызова. Включается только осознанно через env-var.
"""
from unittest.mock import patch, AsyncMock

import pytest


@pytest.mark.asyncio
async def test_async_process_incident_skips_when_disabled():
    """Default state: pipeline disabled → return skipped, никаких LLM."""
    from app.config import settings
    from app.workers.tasks import async_process_incident

    with patch.object(settings, "LLM_PIPELINE_ENABLED", False), \
         patch("app.workers.tasks.IncidentPipeline") as mock_pipeline, \
         patch("app.workers.tasks.SessionLocal"):
        result = await async_process_incident({
            "incident_id": "test-disabled-1",
            "namespace": "prod-kingdom1",
            "labels": {"alertname": "X"},
        })
    assert result == {"status": "skipped", "reason": "LLM_PIPELINE_ENABLED=false"}
    # IncidentPipeline никогда не должен инстанциироваться.
    mock_pipeline.assert_not_called()


@pytest.mark.asyncio
async def test_async_process_incident_runs_when_enabled():
    """LLM_PIPELINE_ENABLED=true → IncidentPipeline.run() вызывается."""
    from app.config import settings
    from app.workers.tasks import async_process_incident

    mock_run = AsyncMock(return_value=None)
    fake_pipeline = type("P", (), {"run": mock_run})()
    with patch.object(settings, "LLM_PIPELINE_ENABLED", True), \
         patch("app.workers.tasks.IncidentPipeline", return_value=fake_pipeline), \
         patch("app.workers.tasks.SessionLocal") as mock_session:
        # incident record не нужен — pipeline-mock не использует.
        mock_session.return_value.query.return_value.filter.return_value.first.return_value = None
        await async_process_incident({
            "incident_id": "test-enabled-1",
            "namespace": "prod-kingdom1",
            "labels": {"alertname": "X"},
        })
    mock_run.assert_awaited_once()


def test_setting_default_is_false():
    """Critical invariant: default LLM_PIPELINE_ENABLED=False.
    Если кто-то поменяет default на True — этот тест упадёт и попадёт
    в code review. Это намеренный safeguard."""
    from app.config import Settings
    fresh = Settings()
    assert fresh.LLM_PIPELINE_ENABLED is False, (
        "LLM_PIPELINE_ENABLED default ДОЛЖЕН быть False — это hard-gate "
        "от случайного LLM-burn. Включение делается только через env-var."
    )
