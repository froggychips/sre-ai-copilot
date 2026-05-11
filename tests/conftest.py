import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Required by pydantic Settings — without these app.config raises on import,
# which blocks test collection. Real values are still required for any
# test that actually hits the network (none of ours do; tests mock at the
# llm_client boundary).
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("DISCORD_WEBHOOK_URL", "https://example.com/test-webhook")
