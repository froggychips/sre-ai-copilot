import numpy as np
from typing import Dict, List
from app.core.reasoning.evidence import EvidenceGraph, EvidenceRelationType

class EnergyFunction:
    @staticmethod
    def compute_node_energy(node_id: str, belief: float, graph: EvidenceGraph) -> float:
        """
        E(node) = contradiction_pressure - support_attraction + temporal_decay_penalty
        """
        # Давление противоречий (repulsive force)
        contradictions = graph.get_contradictions(node_id)
        pressure = sum(c.weight * c.causal_strength for c in contradictions)
        
        # Сила поддержки (attractive force)
        supports = graph.get_supporting(node_id)
        attraction = sum(s.weight * s.causal_strength for s in supports)
        
        # Decay penalty (чем старше, тем выше энергия/нестабильность)
        node = graph.nodes[node_id]
        decay = (1.0 - node.freshness_score) * 0.5
        
        return float(pressure - attraction + decay)

    @staticmethod
    def compute_graph_energy(beliefs: Dict[str, float], graph: EvidenceGraph) -> float:
        """Суммарная энергия графа."""
        total = 0.0
        for node_id, belief in beliefs.items():
            total += EnergyFunction.compute_node_energy(node_id, belief, graph)
        
        # λ * graph_inconsistency (штраф за неконсистентность)
        inconsistency = 0.0
        for edge in graph.edges:
            if edge.relation_type == EvidenceRelationType.CONTRADICTS:
                inconsistency += abs(beliefs.get(edge.source_id, 0) + beliefs.get(edge.target_id, 0))
        
        return total + (0.5 * inconsistency)
