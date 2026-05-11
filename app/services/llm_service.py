from anthropic import AsyncAnthropic
from app.config import settings
from app.services.resilience import llm_retry_strategy
from app.services.claude_cli_service import ClaudeCliService
import logging


class LLMService:
    """LLM-фасад с двумя backend-ами:

    - `anthropic` (default, production) — AsyncAnthropic SDK с ANTHROPIC_API_KEY.
    - `claude_cli` (local PoC / e2e без ключа) — subprocess вокруг
      Claude Code CLI в `--print` режиме, использует CLI-авторизацию.
    """

    def __init__(self) -> None:
        self.backend = (settings.LLM_BACKEND or "anthropic").lower()
        self.model = settings.MODEL_NAME
        if self.backend == "claude_cli":
            self.cli = ClaudeCliService(
                model=settings.MODEL_NAME or None,
                timeout_seconds=float(getattr(settings, "CLAUDE_CLI_TIMEOUT_SECONDS", 180.0)),
            )
            self.client = None
        else:
            self.client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
            self.cli = None

    @llm_retry_strategy
    async def generate_content(self, prompt: str) -> str:
        try:
            if self.backend == "claude_cli":
                return await self.cli.generate_content(prompt)
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=settings.MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(
                block.text
                for block in response.content
                if getattr(block, "type", None) == "text"
            )
            if not text:
                raise ValueError("Empty response from LLM")
            return text
        except Exception as e:
            logging.error(f"LLM call attempt failed: {e}")
            raise


llm_client = LLMService()
