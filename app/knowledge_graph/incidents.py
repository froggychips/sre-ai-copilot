"""Инцидент как детерминированный объект из алертов сервиса.

Без LLM и без эвристик «примерно одновременно». Правило одно: алерт
относится к сервису, у сервиса не больше одного открытого инцидента, значит
алерт относится к нему. Корреляция МЕЖДУ сервисами (общая база, деплой в
окне, вызов через `calls`) — отдельный шаг roadmap, и он придёт сюда как
рёбра между инцидентами, а не как размытие этого правила.

Жизненный цикл:

    алерт fired ──▶ attach_alert ──▶ открытый инцидент сервиса есть? ──да──▶ присоединить
                                            │нет
                                            ▼
                              закрыт < REOPEN_WINDOW_MIN назад? ──да──▶ переоткрыть
                                            │нет
                                            ▼
                                        завести новый

    reconcile_incidents (beat, 5 мин): все алерты resolved → resolved;
    давно ни алерта, ни резолва → aged_out.

Почему переоткрытие, а не новый инцидент: флаппинг (алерт гаснет и через
минуту загорается) для триажа — одно событие; десять инцидентов за час по
одному сервису — шум, который прячет настоящее.

Почему `aged_out`: `alerts_resolve_sync` закрывает алерты по firing-набору
Alertmanager, но при недоступном AM сохраняет их открытыми (safety). Без
старения такой инцидент висел бы вечно и блокировал бы заведение нового.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, cast

import structlog
from sqlalchemy.orm import Session

from app.core.timeutil import ensure_naive
from app.knowledge_graph.schema import AlertEvent, KGIncident
from app.services.alert_noise_rule import NOISE_KIND_RULE

log = structlog.get_logger("kg.incidents")

STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"

RESOLVE_ALL_ALERTS = "all_alerts_resolved"
RESOLVE_AGED_OUT = "aged_out"
#: Шумовой (по статическому правилу) инцидент закрыт, потому что у сервиса
#: появился сигнальный алерт — тот заводит собственный инцидент.
RESOLVE_SUPERSEDED = "superseded_by_signal"

#: Суффикс ключа инцидента, заведённого шумовым алертом. Сигнальный и шумовой
#: алерты одного сервиса нередко имеют одинаковый startsAt (общий такт
#: оценки правил) — без суффикса два инцидента делили бы один ключ.
NOISE_KEY_SUFFIX = "#noise"

#: Алерт после закрытия инцидента в этом окне переоткрывает его.
REOPEN_WINDOW_MIN = 30
#: Открытый инцидент без единого нового алерта столько часов — состарить.
AGE_OUT_HOURS = 7 * 24

_SEVERITY_RANK = {"critical": 3, "warning": 2, "info": 1}


def _now() -> datetime:
    return datetime.utcnow()


def incident_key(namespace: str, service_name: str, opened_at: datetime) -> str:
    return f"{namespace}/{service_name}@{opened_at:%Y%m%dT%H%M%S}"


def _max_severity(a: Optional[str], b: Optional[str]) -> Optional[str]:
    ra = _SEVERITY_RANK.get((a or "").lower(), 0)
    rb = _SEVERITY_RANK.get((b or "").lower(), 0)
    if ra == 0 and rb == 0:
        return a or b
    return a if ra >= rb else b


def open_incident_for(db: Session, namespace: str, service_name: str) -> Optional[KGIncident]:
    return (
        db.query(KGIncident)
        .filter(
            KGIncident.namespace == namespace,
            KGIncident.service_name == service_name,
            KGIncident.status == STATUS_OPEN,
        )
        .one_or_none()
    )


def _recently_resolved(
    db: Session, namespace: str, service_name: str, fired_at: datetime,
) -> Optional[KGIncident]:
    """Закрытый в окне REOPEN_WINDOW_MIN инцидент — кандидат на переоткрытие.

    Чисто шумовые (статическое правило) пропускаем: сигнальный алерт не
    должен оживлять хронический шумовой инцидент недельной давности.
    """
    since = fired_at - timedelta(minutes=REOPEN_WINDOW_MIN)
    rows = (
        db.query(KGIncident)
        .filter(
            KGIncident.namespace == namespace,
            KGIncident.service_name == service_name,
            KGIncident.status == STATUS_RESOLVED,
            KGIncident.resolved_at.isnot(None),
            KGIncident.resolved_at >= since,
        )
        .order_by(KGIncident.resolved_at.desc())
        .limit(5)
        .all()
    )
    for row in rows:
        if not _rule_noise_only(row):
            return row
    return None


def _rule_noise_only(inc: KGIncident) -> bool:
    """Все алерты инцидента — шум ТОЛЬКО по статическому правилу."""
    if not inc.noise:
        return False
    marked = inc.extras.get("noise_fingerprints") if isinstance(inc.extras, dict) else None
    fps = list(inc.fingerprints or [])
    if not fps or not isinstance(marked, dict):
        return False
    return all(list(marked.get(fp) or []) == [NOISE_KIND_RULE] for fp in fps)


def _recent_noise_incident(
    db: Session, namespace: str, service_name: str, fired_at: datetime, hours: float,
) -> Optional[KGIncident]:
    """Последний закрытый чисто шумовой инцидент сервиса в окне `hours`."""
    since = fired_at - timedelta(hours=hours)
    rows = (
        db.query(KGIncident)
        .filter(
            KGIncident.namespace == namespace,
            KGIncident.service_name == service_name,
            KGIncident.status == STATUS_RESOLVED,
            KGIncident.noise.is_(True),
            KGIncident.resolved_at.isnot(None),
            KGIncident.resolved_at >= since,
        )
        .order_by(KGIncident.resolved_at.desc())
        .limit(5)
        .all()
    )
    for row in rows:
        if _rule_noise_only(row):
            return row
    return None


def attach_alert(
    db: Session,
    *,
    namespace: str,
    service_name: str,
    fired_at: datetime,
    alertname: str,
    severity: Optional[str] = None,
    fingerprint: Optional[str] = None,
    service_id: Optional[int] = None,
    noise_kind: Optional[str] = None,
) -> KGIncident:
    """Присоединить алерт к инциденту сервиса (открытому, переоткрытому или новому).

    Идемпотентно по fingerprint: повторный fire того же алерта не меняет
    `alert_count`. Проставляет `kg_alerts.incident_id = incident_key`, если
    строка алерта уже записана (вызывать ПОСЛЕ record_alert_event).

    `noise_kind` — алерт признан шумом ещё на входе (статическое правило,
    см. services/alert_noise_rule). Отличия от обычного пути:
      * пометка шума ставится здесь же, без отдельного mark_incident_noise;
      * нет открытого инцидента → оживляется последний чисто шумовой
        инцидент сервиса за ALERT_NOISE_RULE_REOPEN_HOURS, и это НЕ
        переоткрытие: `reopened_count` не растёт, счётчик флапов — в
        `extras["noise_refires"]`. Итог — один хронический инцидент на
        сервис вместо нового на каждый флап;
      * сигнальный инцидент шумовым алертом не переоткрывается.
    Сигнальный алерт, заставший открытым чисто шумовой инцидент, закрывает
    его (`superseded_by_signal`) и заводит свой — иначе реальная проблема
    получила бы окно и ключ недельного троттлинга.
    """
    fired_at = ensure_naive(fired_at)
    inc = open_incident_for(db, namespace, service_name)
    action = "attached"

    # Тот же fingerprint без пометки = правило перестало совпадать (сменили
    # настройки): это не новый сигнал, инцидент остаётся тем же и теряет флаг.
    known = bool(fingerprint) and fingerprint in (inc.fingerprints or []) if inc is not None else False
    if inc is not None and not noise_kind and not known and _rule_noise_only(inc):
        superseded: Any = inc
        superseded.status = STATUS_RESOLVED
        superseded.resolved_at = max(fired_at, inc.opened_at)
        superseded.resolve_reason = RESOLVE_SUPERSEDED
        # До INSERT нового: частичный уникальный индекс «один открытый на сервис».
        db.flush()
        log.info("kg.incident.noise_superseded", incident=inc.incident_key, alertname=alertname)
        inc = None

    if inc is None and noise_kind:
        from app.config import settings
        hours = float(getattr(settings, "ALERT_NOISE_RULE_REOPEN_HOURS", AGE_OUT_HOURS))
        inc = _recent_noise_incident(db, namespace, service_name, fired_at, hours)
        if inc is not None:
            refired: Any = inc
            refired.status = STATUS_OPEN
            refired.resolved_at = None
            refired.resolve_reason = None
            ex: Dict[str, Any] = dict(inc.extras) if isinstance(inc.extras, dict) else {}
            ex["noise_refires"] = int(ex.get("noise_refires") or 0) + 1
            refired.extras = ex
            action = "noise_refired"
    elif inc is None:
        inc = _recently_resolved(db, namespace, service_name, fired_at)
        if inc is not None:
            reopened: Any = inc
            reopened.status = STATUS_OPEN
            reopened.resolved_at = None
            reopened.resolve_reason = None
            reopened.reopened_count = (inc.reopened_count or 0) + 1
            action = "reopened"

    if inc is None:
        inc = KGIncident(
            incident_key=incident_key(namespace, service_name, fired_at)
            + (NOISE_KEY_SUFFIX if noise_kind else ""),
            namespace=namespace,
            service_name=service_name,
            service_id=service_id,
            status=STATUS_OPEN,
            severity=severity,
            opened_at=fired_at,
            last_alert_at=fired_at,
            alert_count=0,
            alertnames=[],
            fingerprints=[],
            reopened_count=0,
        )
        db.add(inc)
        action = "opened"

    row = inc
    # JSON-колонки: присваиваем НОВЫЙ список — мутация in-place SQLAlchemy
    # без MutableList не заметит.
    fps: List[str] = list(inc.fingerprints or [])
    if fingerprint and fingerprint not in fps:
        fps.append(fingerprint)
    row.fingerprints = fps
    names: List[str] = list(inc.alertnames or [])
    if alertname and alertname not in names:
        names.append(alertname)
    row.alertnames = names
    row.alert_count = len(fps) if fps else (inc.alert_count or 0) + 1
    row.severity = _max_severity(cast(Optional[str], inc.severity), severity)
    if fired_at > inc.last_alert_at:
        row.last_alert_at = fired_at
    if fired_at < inc.opened_at:
        # Алерт пришёл с опозданием и был раньше первого — окно честнее
        # расширить, ключ при этом не трогаем: на него уже ссылаются.
        row.opened_at = fired_at
    if service_id and not inc.service_id:
        row.service_id = service_id
    if fingerprint:
        _sync_rule_mark(row, fingerprint, noise_kind)
    # Шум — свойство набора алертов: новый алерт без пометки шума снимает флаг.
    row.noise = _all_noise(inc.extras, fps)
    db.flush()

    if fingerprint:
        db.query(AlertEvent).filter(AlertEvent.fingerprint == fingerprint).update(
            {"incident_id": inc.incident_key}, synchronize_session=False,
        )

    # `attached` — каждое повторное уведомление AM по уже открытому инциденту
    # (замер 18.11.2026: ~3500 строк за 6 ч на одном CPUThrottlingHigh), как и
    # оживление шумового. В info остаются смены состояния: opened / reopened.
    emit = log.debug if action in ("attached", "noise_refired") else log.info
    emit(
        "kg.incident." + action,
        incident=inc.incident_key, alertname=alertname, alerts=inc.alert_count,
        noise=bool(inc.noise),
    )
    return inc


def _sync_rule_mark(row: Any, fingerprint: str, noise_kind: Optional[str]) -> None:
    """Привести пометку статического правила у fingerprint к текущему решению.

    Правило пересчитывается на каждом уведомлении: поставить вид, если алерт
    под правилом, и снять, если правило больше не совпадает (сменили
    настройки) — иначе однажды помеченный алерт прятал бы инцидент навсегда.
    Остальные виды шума (rollout, meta, gen-mismatch) не трогаются.
    """
    extras: Dict[str, Any] = dict(row.extras) if isinstance(row.extras, dict) else {}
    marked: Dict[str, List[str]] = dict(extras.get("noise_fingerprints") or {})
    prev = list(marked.get(fingerprint) or [])
    kinds = [k for k in prev if k != NOISE_KIND_RULE]
    if noise_kind:
        kinds = sorted(set(kinds) | {noise_kind})
    if kinds == prev:
        return
    if kinds:
        marked[fingerprint] = kinds
    else:
        marked.pop(fingerprint, None)
    extras["noise_fingerprints"] = marked
    if noise_kind:
        extras["noise_marked_at"] = _now().isoformat()
    row.extras = extras                      # новый dict — иначе JSON-мутацию не заметят


def reconcile_incidents(db: Session, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Закрыть инциденты, у которых все алерты resolved; состарить брошенные.

    Возвращает счётчики для контракта source_status: `checked` — сколько
    открытых просмотрено (observed), остальное — что с ними стало.
    """
    now = ensure_naive(now or _now())
    stats: Dict[str, Any] = {"checked": 0, "still_open": 0, "resolved": 0, "aged_out": 0}
    open_incidents = db.query(KGIncident).filter(KGIncident.status == STATUS_OPEN).all()
    age_limit = now - timedelta(hours=AGE_OUT_HOURS)

    for inc in open_incidents:
        row: Any = inc
        stats["checked"] += 1
        fps = _reconcile_fingerprints(inc)
        if fps:
            rows = (
                db.query(AlertEvent.fingerprint, AlertEvent.resolved_at)
                .filter(AlertEvent.fingerprint.in_(fps))
                .all()
            )
            seen = {fp for fp, _ in rows}
            still_firing = [fp for fp, r in rows if r is None]
            missing = set(fps) - seen
            if not still_firing and not missing:
                row.status = STATUS_RESOLVED
                row.resolved_at = max(r for _, r in rows if r is not None)
                row.resolve_reason = RESOLVE_ALL_ALERTS
                stats["resolved"] += 1
                continue
        if inc.last_alert_at < age_limit:
            row.status = STATUS_RESOLVED
            row.resolved_at = now
            row.resolve_reason = RESOLVE_AGED_OUT
            stats["aged_out"] += 1
            continue
        stats["still_open"] += 1

    db.commit()
    if stats["resolved"] or stats["aged_out"]:
        log.info("kg.incidents.reconciled", **stats)
    return stats


def _reconcile_fingerprints(inc: KGIncident) -> List[str]:
    """Алерты, по которым решается, закрывать ли инцидент.

    Шум по статическому правилу, присоединённый к СИГНАЛЬНОМУ инциденту, в
    расчёт не идёт: хронический троттлинг горит неделями и держал бы уже
    погасший инцидент открытым — тот занимал бы единственный открытый слот
    сервиса, и следующая реальная проблема сворачивалась бы в него. После
    закрытия шумовой алерт на ближайшем уведомлении уходит в свой
    хронический инцидент (сигнальный он не переоткрывает).
    У чисто шумового инцидента учитываются все алерты, как раньше.
    """
    fps = list(inc.fingerprints or [])
    marked = inc.extras.get("noise_fingerprints") if isinstance(inc.extras, dict) else None
    if not isinstance(marked, dict):
        return fps
    signal = [fp for fp in fps if NOISE_KIND_RULE not in list(marked.get(fp) or [])]
    return signal or fps


def _all_noise(extras: Any, fps: List[str]) -> bool:
    """True, если у КАЖДОГО алерта инцидента есть виды шума в extras."""
    marked = (extras or {}).get("noise_fingerprints") if isinstance(extras, dict) else None
    if not fps or not isinstance(marked, dict):
        return False
    return all(marked.get(fp) for fp in fps)


def mark_incident_noise(
    db: Session, *, fingerprint: str, kinds: List[str],
) -> Optional[KGIncident]:
    """Записать виды шума для алерта и пересчитать флаг инцидента.

    Решение о шуме принимает обогащение (/enrich-and-forward) ПОСЛЕ приёма
    алерта — поэтому пометка приходит отдельным шагом, а не в attach_alert.
    Инцидент ищется через kg_alerts.incident_id (attach его проставил).
    Нет алерта или инцидента — молча None: шум без инцидента не событие.
    """
    if not fingerprint or not kinds:
        return None
    key = (
        db.query(AlertEvent.incident_id)
        .filter(AlertEvent.fingerprint == fingerprint)
        .scalar()
    )
    if not key:
        return None
    inc = db.query(KGIncident).filter(KGIncident.incident_key == key).one_or_none()
    if inc is None:
        return None
    extras: Dict[str, Any] = dict(inc.extras) if isinstance(inc.extras, dict) else {}
    marked: Dict[str, List[str]] = dict(extras.get("noise_fingerprints") or {})
    prev = list(marked.get(fingerprint) or [])
    marked[fingerprint] = sorted(set(prev) | set(kinds))
    extras["noise_fingerprints"] = marked
    extras["noise_marked_at"] = _now().isoformat()
    row: Any = inc
    row.extras = extras                      # новый dict — иначе JSON-мутацию не заметят
    row.noise = _all_noise(extras, list(inc.fingerprints or []))
    db.flush()
    log.info("kg.incident.noise_marked", incident=inc.incident_key, kinds=kinds, noise=inc.noise)
    return inc


def unmark_incident_noise(db: Session, *, fingerprint: str, kind: str) -> Optional[KGIncident]:
    """Снять у алерта один вид шума и пересчитать флаг инцидента.

    Нужен там, где решение о шуме пересматривается на каждом срабатывании
    (churn GenerationMismatch-а): `mark_incident_noise` только добавляет виды,
    и без снятия однажды помеченный fingerprint прятал бы инцидент и тогда,
    когда Deployment уже в реальном накате. Нечего снимать — None, без записи.
    """
    if not fingerprint or not kind:
        return None
    key = (
        db.query(AlertEvent.incident_id)
        .filter(AlertEvent.fingerprint == fingerprint)
        .scalar()
    )
    if not key:
        return None
    inc = db.query(KGIncident).filter(KGIncident.incident_key == key).one_or_none()
    if inc is None or not isinstance(inc.extras, dict):
        return None
    extras: Dict[str, Any] = dict(inc.extras)
    marked: Dict[str, List[str]] = dict(extras.get("noise_fingerprints") or {})
    kinds = [k for k in (marked.get(fingerprint) or []) if k != kind]
    if kinds == list(marked.get(fingerprint) or []):
        return None
    if kinds:
        marked[fingerprint] = kinds
    else:
        marked.pop(fingerprint, None)
    extras["noise_fingerprints"] = marked
    row: Any = inc
    row.extras = extras
    row.noise = _all_noise(extras, list(inc.fingerprints or []))
    db.flush()
    log.info("kg.incident.noise_unmarked", incident=inc.incident_key, kind=kind, noise=inc.noise)
    return inc


def incident_to_dict(inc: KGIncident) -> Dict[str, Any]:
    extras: Dict[str, Any] = inc.extras if isinstance(inc.extras, dict) else {}
    noise_kinds = sorted({k for ks in (extras.get("noise_fingerprints") or {}).values() for k in (ks or [])})
    return {
        "noise": bool(inc.noise),
        "noise_kinds": noise_kinds,
        "id": inc.id,
        "incident_key": inc.incident_key,
        "namespace": inc.namespace,
        "service": inc.service_name,
        "service_id": inc.service_id,
        "status": inc.status,
        "severity": inc.severity,
        "opened_at": inc.opened_at,
        "last_alert_at": inc.last_alert_at,
        "resolved_at": inc.resolved_at,
        "resolve_reason": inc.resolve_reason,
        "alert_count": inc.alert_count,
        "alertnames": list(inc.alertnames or []),
        "fingerprints": list(inc.fingerprints or []),
        "reopened_count": inc.reopened_count or 0,
    }
