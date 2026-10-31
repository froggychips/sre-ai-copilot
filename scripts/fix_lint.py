#!/usr/bin/env python3
"""
Auto-fix common lint errors
"""
from pathlib import Path

# Map of file paths to fixes
FIXES = {
    "app/observability/ai_metrics.py": [
        (
            "from prometheus_client import Summary\n\nfrom app.models import IncidentRecord",
            "from app.models import IncidentRecord",
        ),
    ],
    "app/queue/worker.py": [
        ("import time\n\nfrom app.agents.analyzer import AnalyzerAgent", "from app.agents.analyzer import AnalyzerAgent"),
        ("    analyzer = AnalyzerAgent()", "    # analyzer = AnalyzerAgent()  # TODO: Use this"),
    ],
    "app/services/audit_logger.py": [
        ("import json\n\nimport structlog", "import structlog"),
    ],
    "app/services/llm_service.py": [
        (
            "from anthropic import AsyncAnthropic\nfrom app.config import settings\nfrom app.services.resilience import llm_retry_strategy\nfrom app.services.claude_cli_service import ClaudeCliService\nimport logging\nimport asyncio\nfrom httpx import TimeoutException, AsyncClient",
            "import asyncio\nimport logging\n\nfrom anthropic import AsyncAnthropic\n\nfrom app.config import settings\nfrom app.services.claude_cli_service import ClaudeCliService\nfrom app.services.resilience import llm_retry_strategy",
        ),
    ],
}


def apply_fixes():
    workspace_root = Path(__file__).parent.parent
    for filepath, fix_list in FIXES.items():
        full_path = workspace_root / filepath
        if not full_path.exists():
            print(f"⚠ {filepath} not found")
            continue

        content = full_path.read_text()
        modified = False

        for old_text, new_text in fix_list:
            if old_text in content:
                content = content.replace(old_text, new_text)
                modified = True
                print(f"✓ Fixed {filepath}")
            else:
                print(f"⚠ Pattern not found in {filepath}: {old_text[:50]}...")

        if modified:
            full_path.write_text(content)


if __name__ == "__main__":
    apply_fixes()
    print("\n✓ All fixable lint errors corrected")
