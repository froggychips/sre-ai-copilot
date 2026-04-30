import google.generativeai as genai
from app.config import settings
from app.services.resilience import llm_retry_strategy
import logging

class GeminiService:
    def __init__(self):
        genai.configure(api_key=settings.GEMINI_API_KEY)
        self.model = genai.GenerativeModel(settings.MODEL_NAME)

    @llm_retry_strategy
    async def generate_content(self, prompt: str) -> str:
        try:
            response = self.model.generate_content(prompt)
            if not response.text:
                raise ValueError("Empty response from Gemini")
            return response.text
        except Exception as e:
            # Логируем ошибку, декоратор сам решит, делать ли ретрай
            logging.error(f"Gemini call attempt failed: {e}")
            raise

gemini_client = GeminiService()
