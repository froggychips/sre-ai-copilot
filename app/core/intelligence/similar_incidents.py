from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.database import IncidentRecord, SessionLocal

# Строки-маркеры unresolved cause из старых записей (до quality gate).
# Нужны для backward-совместимости: записи до этого коммита содержат
# полный текст "No hypothesis survived..." в поле cause.
_UNRESOLVED_CAUSE_PREFIXES = (
    "No hypothesis survived",
    "Manual triage required",
)

# Инцидент в этом окне с тем же cause + resolved = рецидив.
RECURRENCE_WINDOW_DAYS = 7


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


def _extract_service_ns(data: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Извлекает (service, namespace) из обоих форматов инцидент-данных.

    Старый формат: data["targets"][0]["service"] / ["namespace"]
    Новый формат:  data["labels"]["service"] + data["namespace"]
    """
    service = (
        (data.get("targets") or [{}])[0].get("service")
        or (data.get("labels") or {}).get("service")
    )
    namespace = (
        (data.get("targets") or [{}])[0].get("namespace")
        or data.get("namespace")
    )
    return service, namespace


class SimilarIncidentEngine:
    @staticmethod
    def find(current_incident: Dict[str, Any], limit: int = 3) -> List[Dict[str, Any]]:
        """Ищет похожие инциденты в истории на основе детерминированного скоринга.

        Пропускает записи с unresolved cause (no_survivor / manual triage) —
        они не несут полезного сигнала и засоряют past_bullets в промптах.

        Возвращаемые dict-ы содержат поле `recurrence: bool`:
            True — тот же сервис, та же причина, resolved в последние
            RECURRENCE_WINDOW_DAYS дней. FixAgent должен сменить тактику.
        """
        # try/finally вокруг тела: при исключении внутри скоринга сессия
        # раньше текла (db.close() стоял только в happy-path в конце).
        db = SessionLocal()
        try:
            history = (
                db.query(IncidentRecord)
                .filter(IncidentRecord.is_accepted == "ACCEPTED")
                .all()
            )

            recurrence_cutoff = datetime.utcnow() - timedelta(days=RECURRENCE_WINDOW_DAYS)
            current_service, current_ns = _extract_service_ns(current_incident)
            current_cause = current_incident.get("root_cause")

            matches = []
            for record in history:
                hist_data: dict[str, Any] = record.data or {}
                hist_analysis: dict[str, Any] = record.analysis or {}

                # KG quality gate: пропускаем записи без actionable cause.
                cause = hist_analysis.get("cause")
                resolution_quality = hist_analysis.get("resolution_quality")
                if not _is_quality_cause(cause, resolution_quality):
                    continue

                hist_service, hist_ns = _extract_service_ns(hist_data)

                score = 0.0

                # 1. Совпадение сервиса
                if hist_service and hist_service == current_service:
                    score += 0.4

                # 2. Совпадение причины
                if cause == current_cause:
                    score += 0.4

                # 3. Совпадение неймспейса
                if hist_ns and hist_ns == current_ns:
                    score += 0.2

                if score <= 0.4:
                    continue

                # Recurrence: тот же сервис, resolved в пределах окна.
                is_recurrence = bool(
                    hist_service
                    and hist_service == current_service
                    and record.created_at is not None
                    and record.created_at >= recurrence_cutoff
                    and (
                        record.status == "RESOLVED"
                        or resolution_quality == "resolved"
                    )
                )

                matches.append(
                    {
                        "incident_id": record.incident_id,
                        "score": round(score, 2),
                        "root_cause": cause,
                        "summary": (hist_analysis.get("summary") or "")[:100] + "...",
                        "recurrence": is_recurrence,
                        "days_ago": (
                            (datetime.utcnow() - record.created_at).days
                            if record.created_at else None
                        ),
                    }
                )

            return sorted(matches, key=lambda x: x["score"], reverse=True)[:limit]
        finally:
            db.close()
