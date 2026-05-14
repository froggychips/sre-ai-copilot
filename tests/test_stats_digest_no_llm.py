"""Инвариант: stats_digest НЕ дёргает LLM ни прямо, ни через импорты.

Daily-digest по определению должен быть pure data-aggregation:
  - cluster_health через VictoriaMetrics REST
  - KG-данные через SQL к postgres
  - stale-deployments через kubectl get
  - финал — `httpx.post` в Discord webhook

Любое появление `LLMService`, `AnalyzerAgent`, `FixAgent`, `MultiHypothesisAgent`,
`SynthesisAgent`, `FactCriticAgent`, `RiskAgent`, `generate_content`
в исходнике `stats_digest.py` = баг. Этот тест fail-ит при таком регрессе
и предотвращает случайное добавление LLM-вызова в analytical-task.
"""
from pathlib import Path

_LLM_FORBIDDEN_TOKENS = (
    # LLM-фасад и retry-стратегия
    "LLMService",
    "llm_service",
    "llm_client",
    "generate_content",
    # Reasoning-агенты
    "AnalyzerAgent",
    "MultiHypothesisAgent",
    "FactCriticAgent",
    "FixAgent",
    "RiskAgent",
    "SynthesisAgent",
    # Anthropic SDK
    "AsyncAnthropic",
    "anthropic.AsyncAnthropic",
    # CLI-backend
    "ClaudeCliService",
    "claude_cli_service",
)


def test_stats_digest_source_does_not_reference_llm():
    src_path = Path("app/services/stats_digest.py")
    assert src_path.exists(), f"missing source: {src_path}"
    src = src_path.read_text()
    forbidden_found = [t for t in _LLM_FORBIDDEN_TOKENS if t in src]
    assert not forbidden_found, (
        f"stats_digest.py содержит запрещённые LLM-ссылки: {forbidden_found}. "
        f"Daily-digest должен быть pure data-aggregation. См. модульный docstring."
    )


def test_stats_digest_does_not_import_agents_or_pipeline():
    """Дополнительный гейт: нет import из app.agents и app.workers.pipeline."""
    src = Path("app/services/stats_digest.py").read_text()
    forbidden_imports = [
        "from app.agents",
        "import app.agents",
        "from app.workers.pipeline",
        "from app.services.llm_service",
        "from app.services.claude_cli_service",
    ]
    found = [imp for imp in forbidden_imports if imp in src]
    assert not found, (
        f"stats_digest.py содержит запрещённые импорты: {found}. "
        f"Аналитика не должна пересекаться с reasoning-пайплайном."
    )
