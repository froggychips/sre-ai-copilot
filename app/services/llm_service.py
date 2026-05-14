import asyncio
import logging
from typing import Any, Dict, Optional

from anthropic import AsyncAnthropic

from app.config import settings
from app.services.claude_cli_service import ClaudeCliService
from app.services.resilience import llm_retry_strategy


class LLMService:
    """LLM-фасад с двумя backend-ами:

    - `anthropic` (default, production) — AsyncAnthropic SDK с ANTHROPIC_API_KEY.
    - `claude_cli` (local PoC / e2e без ключа) — subprocess вокруг
      Claude Code CLI в `--print` режиме, использует CLI-авторизацию.

    Контракт:
      generate_content(prompt) -> str
        Legacy/simple — возвращает только text. Backward-compat для прямых
        callers.
      generate_full(prompt) -> dict
        Возвращает {text, input_tokens, output_tokens, model, backend}.
        Используется BaseAgent.ask() для записи реальных token-usage в
        Prometheus per agent. Для claude_cli backend usage=None
        (subprocess не возвращает usage info).
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
    async def generate_full(self, prompt: str) -> Dict[str, Any]:
        """Single LLM round-trip с возвратом usage-info.

        Возвращает:
          {text: str, input_tokens: int, output_tokens: int,
           model: str, backend: str}
        Для claude_cli (subprocess без usage API) input/output_tokens = 0
        (правильнее чем char-approximation — counter с 0 не искажает sum).
        """
        try:
            if self.backend == "claude_cli":
                assert self.cli is not None
                text = await self.cli.generate_content(prompt)
                return {
                    "text": text,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "model": self.model,
                    "backend": "claude_cli",
                }
            assert self.client is not None
            llm_timeout = getattr(settings, "LLM_TIMEOUT_SECONDS", 30.0)
            response = await asyncio.wait_for(
                self.client.messages.create(
                    model=self.model,
                    max_tokens=settings.MAX_TOKENS,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=llm_timeout,
            )
            # Anthropic ContentBlock = TextBlock | ToolUseBlock; .text только у TextBlock.
            text = "".join(
                getattr(block, "text", "")
                for block in response.content
                if getattr(block, "type", None) == "text"
            )
            if not text:
                raise ValueError("Empty response from LLM")
            # Anthropic SDK: response.usage.input_tokens / .output_tokens
            usage = getattr(response, "usage", None)
            return {
                "text": text,
                "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
                "model": self.model,
                "backend": "anthropic",
            }
        except asyncio.TimeoutError:
            logging.error("LLM call timed out")
            raise ValueError("LLM timeout")
        except Exception as e:
            logging.error(f"LLM call attempt failed: {e}")
            raise

    async def generate_content(self, prompt: str) -> str:
        """Backward-compat: возвращает только text. Внутри идёт через generate_full."""
        return (await self.generate_full(prompt))["text"]


llm_client = LLMService()
