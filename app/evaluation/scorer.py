from app.llm.router import ModelRouter

class Scorer:
    @staticmethod
    async def evaluate_fix(fix_suggestion: str, context: dict) -> float:
        """
        Использует 'strong' модель для оценки качества предложенного фикса.
        Возвращает скор от 0.0 до 1.0.
        """
        prompt = f"Evaluate this Kubernetes fix: {fix_suggestion}. Context: {context}. Score (0.0-1.0) and brief reason."
        evaluation = await ModelRouter.route_and_call("analysis", prompt, complexity="strong")
        # В реальной системе здесь парсинг JSON-ответа модели
        return 0.9 # Placeholder
