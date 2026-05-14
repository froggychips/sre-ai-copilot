class ModelRouter:
    """Routes model calls by task type.

    For now, all task types use the shared LLM service client.
    """

    @staticmethod
    async def route_and_call(task_type: str, prompt: str) -> str:
        """Backward-compat — text-only."""
        _ = task_type
        from app.services.llm_service import llm_client
        return await llm_client.generate_content(prompt)

    @staticmethod
    async def route_and_call_full(task_type: str, prompt: str):
        """Возвращает dict с {text, input_tokens, output_tokens, model, backend}.
        Используется BaseAgent для real-token attribution per agent."""
        _ = task_type
        from app.services.llm_service import llm_client
        return await llm_client.generate_full(prompt)
