import numpy as np
from typing import Dict
from .energy.energy_function import EnergyFunction
from app.core.reasoning.evidence import EvidenceGraph

class BeliefEquilibriumEngine:
    def __init__(self, graph: EvidenceGraph, learning_rate: float = 0.1):
        self.graph = graph
        self.lr = learning_rate
        # b ∈ R^n (Vector Field State)
        self.beliefs: Dict[str, float] = {
            eid: e.confidence * 2 - 1 for eid, e in graph.nodes.items()
        }

    def compute_gradient(self) -> Dict[str, float]:
        """Вычисляет градиент энергии ∇E(b)."""
        eps = 1e-5
        gradient = {}
        for node_id in self.graph.nodes:
            # Численное приближение градиента: (E(b+eps) - E(b)) / eps
            e_base = EnergyFunction.compute_graph_energy(self.beliefs, self.graph)
            self.beliefs[node_id] += eps
            e_plus = EnergyFunction.compute_graph_energy(self.beliefs, self.graph)
            self.beliefs[node_id] -= eps
            gradient[node_id] = (e_plus - e_base) / eps
        return gradient

    def step(self):
        """b_{t+1} = b_t - η * ∇E(b_t)"""
        grad = self.compute_gradient()
        for node_id, g in grad.items():
            self.beliefs[node_id] = np.clip(self.beliefs[node_id] - self.lr * g, -1.0, 1.0)

    def solve(self, max_iter: int = 100, tol: float = 1e-4):
        """Минимизация энергии до сходимости (плато)."""
        for i in range(max_iter):
            grad = self.compute_gradient()
            grad_norm = np.linalg.norm(list(grad.values()))
            
            if grad_norm < tol:
                break
                
            self.step()

    def detect_equilibrium(self) -> bool:
        grad = self.compute_gradient()
        return np.linalg.norm(list(grad.values())) < 1e-3
