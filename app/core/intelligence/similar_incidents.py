from typing import Any, Dict, List, Optional

from app.database import IncidentRecord, SessionLocal

# Строки-маркеры unresolved cause из старых записей (до quality gate).
# Нужны для backward-совместимости: записи до этого коммита содержат
# полный текст "No hypothesis survived..." в поле cause.
_UNRESOLVED_CAUSE_PREFIXES = (
    "No hypothesis survived",
    "Manual triage required",
)


def _is_quality_cause(cause: Optional[str], resolution_quality: Optional[str]) -> bool:
    """True если причина пригодна для KG-retrieval."""
    if resolution_quality == "unresolved":
        return False
    if not cause:
        return False
    for prefix in _UNRESOLVED_CAUSE_PREFIXES:
        if cause.startswith(prefix):
            return False
    return True


class SimilarIncidentEngine:
    @staticmethod
    def find(current_incident: Dict[str, Any], limit: int = 3) -> List[Dict[str, Any]]:
        """Ищет похожие инциденты в истории на основе детерминированного скоринга.

        Пропускает записи с unresolved cause (no_survivor / manual triage) —
        они не несут полезного сигнала и засоряют past_bullets в промптах.
        """
        db = SessionLocal()
        history = (
            db.query(IncidentRecord)
            .filter(IncidentRecord.is_accepted == "ACCEPTED")
            .all()
        )

        matches = []
        current_service = current_incident.get("targets", [{}])[0].get("service")
        current_cause = current_incident.get("root_cause")

        for record in history:
            hist_data = record.data or {}
            hist_analysis = record.analysis or {}

            # KG quality gate: пропускаем записи без actionable cause.
            cause = hist_analysis.get("cause")
            resolution_quality = hist_analysis.get("resolution_quality")
            if not _is_quality_cause(cause, resolution_quality):
                continue

            score = 0.0

            # 1. Совпадение сервиса
            if hist_data.get("targets", [{}])[0].get("service") == current_service:
                score += 0.4

            # 2. Совпадение причины
            if cause == current_cause:
                score += 0.4

            # 3. Совпадение неймспейса
            if hist_data.get("targets", [{}])[0].get(
                "namespace"
            ) == current_incident.get("targets", [{}])[0].get("namespace"):
                score += 0.2

            if score > 0.4:
                matches.append(
                    {
                        "incident_id": record.incident_id,
                        "score": round(score, 2),
                        "root_cause": cause,
                        "summary": hist_analysis.get("summary", "")[:100] + "...",
                    }
                )

        db.close()
        return sorted(matches, key=lambda x: x["score"], reverse=True)[:limit]
