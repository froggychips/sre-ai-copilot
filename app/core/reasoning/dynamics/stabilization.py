import numpy as np
from typing import Dict

class BeliefStabilizationLayer:
    def __init__(self, alpha: float = 0.3):
        self.alpha = alpha  # Damping factor (скорость адаптации)

    def apply(self, prev_beliefs: Dict[str, float], new_beliefs: Dict[str, float]) -> Dict[str, float]:
        """
        Применяет экспоненциальное затухание для плавного обновления состояний.
        b_t = (1 - α) * b_prev + α * b_new
        """
        stabilized = {}
        all_keys = set(prev_beliefs.keys()) | set(new_beliefs.keys())
        
        for key in all_keys:
            prev = prev_beliefs.get(key, 0.0)
            new = new_beliefs.get(key, 0.0)
            stabilized[key] = (1 - self.alpha) * prev + self.alpha * new
            
        return stabilized

    @staticmethod
    def normalize_mass(beliefs: Dict[str, float]) -> Dict[str, float]:
        """
        Нормализация, чтобы суммарная масса убеждений (энергия графа) оставалась ограниченной.
        """
        if not beliefs: return beliefs
        values = np.array(list(beliefs.values()))
        # Масштабируем так, чтобы L2-норма была постоянной, если это необходимо
        norm = np.linalg.norm(values)
        if norm > 1.0:
            values = values / norm
        return dict(zip(beliefs.keys(), values))
