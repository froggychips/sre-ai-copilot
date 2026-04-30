from app.services.gemini_service import gemini_client
import json

class BaseAgent:
    def __init__(self, name: str, role: str):
        self.name = name
        self.role = role

    async def ask(self, prompt: str) -> str:
        full_prompt = f"Role: {self.role}\n\nTask: {prompt}"
        return await gemini_client.generate_content(full_prompt)
