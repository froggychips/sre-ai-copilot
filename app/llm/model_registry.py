from dataclasses import dataclass

@dataclass
class ModelConfig:
    name: str
    tier: str # cheap, medium, strong

class ModelRegistry:
    # Определение доступных моделей
    MODELS = {
        "cheap": ModelConfig(name="gemini-1.5-flash", tier="cheap"),
        "medium": ModelConfig(name="gemini-1.5-pro", tier="medium"),
        "strong": ModelConfig(name="gemini-1.5-pro-latest", tier="strong"),
    }

    @classmethod
    def get_model(cls, tier: str) -> ModelConfig:
        return cls.MODELS.get(tier, cls.MODELS["medium"])
