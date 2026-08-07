from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.database import IncidentRecord, SessionLocal

# Строки-маркеры unresolved cause из старых записей (до quality gate).
# Нужны для backward-совместимости: записи до этого коммита содержат
# полный текст "No hypothesis survived..." в поле cause.
_UNRESOLVED_CAUSE_PREFIXES = (
    "No hypothesis survived",
    "Manual triage required",
)

# Инцидент в этом окне с реальным сигналом сходства + resolved = рецидив.
RECURRENCE_WINDOW_DAYS = 7

# Границы скана истории. Раньше query был unbounded full scan всех ACCEPTED
# записей на КАЖДЫЙ инцидент — на живой инсталляции это растёт бесконечно.
# getattr с дефолтом, чтобы не трогать config.py (см. правило батча).
_DEFAULT_SCAN_LIMIT = 500
_DEFAULT_LOOKBACK_DAYS = 90


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


def _extract_alertname(data: Dict[str, Any]) -> Optional[str]:
    """alertname из обоих форматов инцидент-данных (labels / targets)."""
    return (
        (data.get("labels") or {}).get("alertname")
        or (data.get("targets") or [{}])[0].get("alertname")
    )


class SimilarIncidentEngine:
    @staticmethod
    def find(current_incident: Dict[str, Any], limit: int = 3) -> List[Dict[str, Any]]:
        """Ищет похожие инциденты в истории на основе детерминированного скоринга.

        Пропускает записи с unresolved cause (no_survivor / manual triage) —
        они не несут полезного сигнала и засоряют past_bullets в промптах.

        Скоринг: service +0.4, alertname +0.3, cause +0.4, namespace +0.2;
        порог score > 0.4. `root_cause` текущего инцидента на stage_hypothesize
        ещё не существует (RCA не отработала) — поэтому alertname обязателен
        как реальный сигнал сходства, иначе матчинг схлопывается в
        «тот же сервис + namespace».

        Возвращаемые dict-ы содержат поле `recurrence: bool`:
            True — тот же сервис И реальный сигнал сходства (тот же alertname
            ИЛИ та же причина), resolved в последние RECURRENCE_WINDOW_DAYS
            дней. Явно РАЗНЫЕ alertname-ы блокируют рецидив: раньше хватало
            «same service, any RESOLVED за 7 дней», и любой активный сервис
            делал каждый новый несвязанный alert «рецидивом», систематически
            смещая FixAgent через _RECURRENCE_PREFIX. Legacy-записи без
            alertname в data остаются на старой service-семантике
            (backward compat с накопленной историей).
        """
        scan_limit = int(getattr(settings, "SIMILAR_INCIDENTS_SCAN_LIMIT",
                                 _DEFAULT_SCAN_LIMIT))
        lookback_days = int(getattr(settings, "SIMILAR_INCIDENTS_LOOKBACK_DAYS",
                                    _DEFAULT_LOOKBACK_DAYS))

        # try/finally вокруг тела: при исключении внутри скоринга сессия
        # раньше текла (db.close() стоял только в happy-path в конце).
        db = SessionLocal()
        try:
            history_cutoff = datetime.utcnow() - timedelta(days=lookback_days)
            history = (
                db.query(IncidentRecord)
                .filter(
                    IncidentRecord.is_accepted == "ACCEPTED",
                    IncidentRecord.created_at >= history_cutoff,
                )
                .order_by(IncidentRecord.created_at.desc())
                .limit(scan_limit)
                .all()
            )

            recurrence_cutoff = datetime.utcnow() - timedelta(days=RECURRENCE_WINDOW_DAYS)
            current_service, current_ns = _extract_service_ns(current_incident)
            current_alertname = _extract_alertname(current_incident)
            # На stage_hypothesize root_cause текущего инцидента всегда None —
            # компонент оживает только для вызовов после RCA (replay/analytics).
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
                hist_alertname = _extract_alertname(hist_data)

                same_service = bool(hist_service and hist_service == current_service)
                same_alertname = bool(
                    hist_alertname
                    and current_alertname
                    and hist_alertname == current_alertname
                )
                same_cause = bool(cause and current_cause and cause == current_cause)

                score = 0.0
                if same_service:
                    score += 0.4
                if same_alertname:
                    score += 0.3
                if same_cause:
                    score += 0.4
                if hist_ns and hist_ns == current_ns:
                    score += 0.2

                if score <= 0.4:
                    continue

                # Recurrence: тот же сервис + сигнал сходства, resolved в
                # пределах окна. Явно РАЗНЫЕ alertname-ы блокируют рецидив
                # (несвязанный alert того же сервиса); когда alertname
                # неизвестен с одной из сторон (legacy-записи, ad-hoc
                # вызовы) — не блокируем, сохраняя старую семантику.
                alertname_known = bool(hist_alertname and current_alertname)
                similarity_signal = (
                    same_cause
                    or (alertname_known and same_alertname)
                    or not alertname_known
                )
                is_recurrence = bool(
                    same_service
                    and similarity_signal
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

            return sorted(matches, key=lambda x: x["score"], reverse=True)[:limit]  # type: ignore[return-value]
        finally:
            db.close()
