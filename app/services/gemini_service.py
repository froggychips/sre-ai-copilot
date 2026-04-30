import google.generativeai as genai
from app.config import settings

class GeminiService:
    def __init__(self):
        genai.configure(api_key=settings.GEMINI_API_KEY)
        self.model = genai.GenerativeModel('gemini-pro')

    async def generate_content(self, prompt: str) -> str:
        response = self.model.generate_content(prompt)
        return response.text

gemini_client = GeminiService()
