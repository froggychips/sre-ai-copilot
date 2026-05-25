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


# ──────────────────────────────────────────────────────────────────────────
# Conditional Postgres marker
# ──────────────────────────────────────────────────────────────────────────
# Несколько integration-тестов (alertmanager store/enrich, e2e smoke)
# используют `app.database.engine` напрямую, который читает settings.DATABASE_URL.
# На self-hosted CI runner'е (jabbook-air-m3, macOS arm64) postgres не
# поднят, и `services:` блок GitHub Actions недоступен (only ubuntu-latest).
# Эти тесты должны skip'аться когда нет живого postgres — иначе CI падает
# и merges идут через `--admin` bypass, что разрушает дисциплину review.
#
# Для опт-ина (локально или после добавления docker-pre-step) достаточно:
#   export DATABASE_URL=postgresql://user:pass@localhost:5432/db
# Если URL парсится и `psycopg2.connect` отвечает — тесты прогоняются.
def _has_live_postgres() -> bool:
    """True если settings.DATABASE_URL указывает на живой postgres.

    Проверяем именно через psycopg2.connect, а не только по URL — на
    self-hosted runner'е default-URL из config.py выглядит как
    postgresql://..., но порт 5432 не слушает никто.
    """
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        # Без явного DATABASE_URL пользуемся дефолтом из app.config (postgres
        # на localhost). На CI runner'е этого postgres нет.
        try:
            from app.config import settings
            url = settings.DATABASE_URL
        except Exception:
            return False
    if not url.startswith(("postgresql://", "postgresql+", "postgres://")):
        return False
    try:
        import psycopg2  # noqa: PLC0415 — lazy import
        conn = psycopg2.connect(url, connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


requires_postgres = pytest.mark.skipif(
    not _has_live_postgres(),
    reason="требуется живой postgres (set DATABASE_URL на доступный инстанс)",
)


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
