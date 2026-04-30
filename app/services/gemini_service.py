import google.generativeai as genai
from app.config import settings
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import logging

class GeminiService:
    def __init__(self):
        genai.configure(api_key=settings.GEMINI_API_KEY)
        self.model = genai.GenerativeModel('gemini-pro')

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        reraise=True
    )
    async def generate_content(self, prompt: str) -> str:
        try:
            response = self.model.generate_content(prompt)
            if not response.text:
                raise ValueError("Empty response from Gemini")
            return response.text
        except Exception as e:
            logging.error(f"Gemini API error: {e}")
            raise

gemini_client = GeminiService()
