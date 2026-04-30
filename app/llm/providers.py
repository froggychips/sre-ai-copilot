import google.generativeai as genai
from app.config import settings

class GeminiProvider:
    def __init__(self, model_name: str):
        self.model = genai.GenerativeModel(model_name)

    async def generate(self, prompt: str) -> str:
        response = self.model.generate_content(prompt)
        return response.text
