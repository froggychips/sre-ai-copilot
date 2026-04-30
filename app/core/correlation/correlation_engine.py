from app.core.graph.incident_graph import IncidentSignalGraph
from app.core.topology.k8s_topology import K8sTopology

class CorrelationEngine:
    def __init__(self, topology: K8sTopology):
        self.topology = topology

    def correlate(self, graph: IncidentSignalGraph):
        # 1. Топологическая корреляция
        topo_map = self.topology.get_service_map("default")
        
        for edge in graph.edges:
            # Усиление связей, если события происходят в одном Deployment
            s_pod = edge["source"].data.get("pod_name")
            t_pod = edge["target"].data.get("pod_name")
            
            # Логика: если поды в одном деплоементе -> boost
            for dep, info in topo_map.items():
                if s_pod in info["pods"] and t_pod in info["pods"]:
                    edge["weight"] = edge.get("weight", 1.0) + 2.0
                    edge["type"] = "topological_proximity"
