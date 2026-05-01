class ModelRouter:
    """Routes model calls by task type.

    For now, all task types use Gemini via the shared service client.
    """

    @staticmethod
    async def route_and_call(task_type: str, prompt: str) -> str:
        # task_type is kept for forward compatibility with multi-model routing.
        _ = task_type
        from app.services.gemini_service import gemini_client

        return await gemini_client.generate_content(prompt)
