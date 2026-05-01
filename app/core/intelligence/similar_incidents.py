from typing import List, Dict, Any
from app.database import SessionLocal, IncidentRecord

class SimilarIncidentEngine:
    @staticmethod
    def find(current_incident: Dict[str, Any], limit: int = 3) -> List[Dict[str, Any]]:
        """
        Ищет похожие инциденты в истории на основе детерминированного скоринга.
        """
        db = SessionLocal()
        history = db.query(IncidentRecord).filter(IncidentRecord.is_accepted == "ACCEPTED").all()
        
        matches = []
        current_service = current_incident.get("targets", [{}])[0].get("service")
        current_cause = current_incident.get("root_cause")

        for record in history:
            score = 0.0
            hist_data = record.data or {}
            hist_analysis = record.analysis or {}
            
            # 1. Совпадение сервиса
            if hist_data.get("targets", [{}])[0].get("service") == current_service:
                score += 0.4
            
            # 2. Совпадение причины
            if hist_analysis.get("cause") == current_cause:
                score += 0.4
            
            # 3. Совпадение неймспейса
            if hist_data.get("targets", [{}])[0].get("namespace") == current_incident.get("targets", [{}])[0].get("namespace"):
                score += 0.2

            if score > 0.4:
                matches.append({
                    "incident_id": record.incident_id,
                    "score": round(score, 2),
                    "root_cause": hist_analysis.get("cause"),
                    "summary": hist_analysis.get("summary", "")[:100] + "..."
                })
        
        db.close()
        return sorted(matches, key=lambda x: x["score"], reverse=True)[:limit]
