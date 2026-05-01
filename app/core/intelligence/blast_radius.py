from typing import List, Dict, Any

class BlastRadiusEngine:
    @staticmethod
    def calculate(incident_data: Dict[str, Any], service_map: Dict[str, Any]) -> Dict[str, Any]:
        """
        Оценивает масштаб влияния инцидента на инфраструктуру.
        """
        target_service = incident_data.get("targets", [{}])[0].get("service")
        
        affected_pods = []
        affected_nodes = []
        
        if target_service in service_map:
            info = service_map[target_service]
            affected_pods = info.get("pods", [])
            affected_nodes = info.get("nodes", [])

        # Определение серьезности (SRE rules)
        severity = "low"
        if len(affected_pods) > 5: severity = "medium"
        if len(set(affected_nodes)) > 1: severity = "high"

        return {
            "pods_count": len(affected_pods),
            "nodes_count": len(set(affected_nodes)),
            "severity": severity,
            "affected_service": target_service
        }
