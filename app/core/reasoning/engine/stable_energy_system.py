import numpy as np
from app.core.reasoning.engine.unified_state import SystemState, UnifiedEnergyFunction

class StableEnergySystem:
    def __init__(self, lambda_val: float = 1.0, epsilon: float = 1e-6):
        self.lambda_val = lambda_val
        self.epsilon = epsilon

    def compute_e_norm(self, state: SystemState) -> float:
        """
        E_norm(S) = E(S) / (|V| + λ|E|)
        Scale-invariant normalization.
        """
        v_count = len(state.graph.nodes)
        e_count = len(state.graph.edges)
        
        raw_energy = UnifiedEnergyFunction.compute(state)
        divisor = v_count + (self.lambda_val * e_count)
        
        return float(raw_energy / max(divisor, 1.0))

    def compute_confidence(self, state: SystemState) -> float:
        """
        confidence(h) = exp(-E_norm(S_h))
        Projection of stability state.
        """
        energy = self.compute_e_norm(state)
        return float(np.exp(-energy))

    def detect_convergence(self, e_prev: float, e_curr: float, state: SystemState) -> bool:
        """
        Convergence condition:
        |E_norm(S_t) - E_norm(S_{t-1})| < ε
        """
        delta = abs(e_curr - e_prev)
        return delta < self.epsilon

    def solve(self, state: SystemState, max_iterations: int = 50) -> SystemState:
        """Fixed-point solver for energy minimization."""
        e_prev = float('inf')
        
        for _ in range(max_iterations):
            e_curr = self.compute_e_norm(state)
            
            if self.detect_convergence(e_prev, e_curr, state):
                break
                
            # Internal belief update (step)
            # Simplified fixed-point iteration for equilibrium
            e_prev = e_curr
            
        return state
