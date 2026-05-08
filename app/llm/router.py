class ModelRouter:
    """Routes model calls by task type.

    For now, all task types use the shared LLM service client.
    """

    @staticmethod
    async def route_and_call(task_type: str, prompt: str) -> str:
        # task_type is kept for forward compatibility with multi-model routing.
        _ = task_type
        from app.services.llm_service import llm_client

        return await llm_client.generate_content(prompt)
