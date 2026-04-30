from app.llm.model_registry import ModelRegistry
from app.llm.providers import GeminiProvider
import structlog

logger = structlog.get_logger()

class ModelRouter:
    @staticmethod
    async def route_and_call(task_type: str, prompt: str, complexity: str = "medium") -> str:
        """
        Выбирает модель на основе типа задачи и сложности.
        """
        # Логика маппинга задач на тиры моделей
        tier_map = {
            "parsing": "cheap",
            "hypothesis": "medium",
            "analysis": "strong",
            "fix": "strong"
        }
        
        tier = tier_map.get(task_type, complexity)
        model_cfg = ModelRegistry.get_model(tier)
        
        logger.info("llm_routing_decision", task=task_type, model=model_cfg.name, tier=tier)
        
        provider = GeminiProvider(model_cfg.name)
        return await provider.generate(prompt)
