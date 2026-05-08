from anthropic import AsyncAnthropic
from app.config import settings
from app.services.resilience import llm_retry_strategy
import logging


class LLMService:
    def __init__(self) -> None:
        self.client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        self.model = settings.MODEL_NAME

    @llm_retry_strategy
    async def generate_content(self, prompt: str) -> str:
        try:
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
