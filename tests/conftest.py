import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Required by pydantic Settings — without these app.config raises on import,
# which blocks test collection. Real values are still required for any
# test that actually hits the network (none of ours do; tests mock at the
# llm_client boundary).
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("DISCORD_WEBHOOK_URL", "https://example.com/test-webhook")


@pytest.fixture(autouse=True)
def enable_llm_pipeline_for_tests(monkeypatch):
    """Pipeline-тесты ожидают что incident-pipeline ВЫПОЛНЯЕТСЯ.

    `LLM_PIPELINE_ENABLED=False` в production — это hard-gate от
    случайного LLM-burn (см. test_llm_pipeline_hard_gate). В тестах
    переопределяем на True по умолчанию.

    Тесты которым нужно проверить именно gate-поведение (Disabled)
    делают свой patch.object(settings, "LLM_PIPELINE_ENABLED", False) —
    он перебивает эту fixture внутри своего scope.
    """
    from app.config import settings
    monkeypatch.setattr(settings, "LLM_PIPELINE_ENABLED", True)
    yield
