import asyncio
import logging
from typing import Optional

from anthropic import AsyncAnthropic

from app.config import settings
from app.services.claude_cli_service import ClaudeCliService
from app.services.resilience import llm_retry_strategy


class LLMService:
    """LLM-фасад с двумя backend-ами:

    - `anthropic` (default, production) — AsyncAnthropic SDK с ANTHROPIC_API_KEY.
    - `claude_cli` (local PoC / e2e без ключа) — subprocess вокруг
      Claude Code CLI в `--print` режиме, использует CLI-авторизацию.
    """

    def __init__(self) -> None:
        self.backend = (settings.LLM_BACKEND or "anthropic").lower()
        self.model = settings.MODEL_NAME
        self.cli: Optional[ClaudeCliService] = None
        self.client: Optional[AsyncAnthropic] = None
        if self.backend == "claude_cli":
            self.cli = ClaudeCliService(
                model=settings.MODEL_NAME or None,
                timeout_seconds=float(
                    getattr(settings, "CLAUDE_CLI_TIMEOUT_SECONDS", 180.0)
                ),
            )
        else:
            self.client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

    @llm_retry_strategy
    async def generate_content(self, prompt: str) -> str:
        try:
            if self.backend == "claude_cli":
                assert self.cli is not None  # __init__ инициализирует под этот backend
                return await self.cli.generate_content(prompt)
            assert self.client is not None  # anthropic-backend гарантирует client
            # Add timeout for LLM calls
            llm_timeout = getattr(settings, "LLM_TIMEOUT_SECONDS", 30.0)
            response = await asyncio.wait_for(
                self.client.messages.create(
                    model=self.model,
                    max_tokens=settings.MAX_TOKENS,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=llm_timeout,
            )
            # Anthropic ContentBlock = TextBlock | ToolUseBlock; .text есть
            # только у TextBlock — getattr с дефолтом и фильтром.
            text = "".join(
                getattr(block, "text", "")
                for block in response.content
                if getattr(block, "type", None) == "text"
            )
            if not text:
                raise ValueError("Empty response from LLM")
            return text
        except asyncio.TimeoutError:
            logging.error("LLM call timed out")
            raise ValueError("LLM timeout")
        except Exception as e:
            logging.error(f"LLM call attempt failed: {e}")
            raise


llm_client = LLMService()
