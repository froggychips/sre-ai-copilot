"""Stuck-alerts escalation: find alerts firing >24h без resolved_at.

Backstory (KG-analytics, 2026-05-23): TTR-аналитика по kg_alerts показала,
что `KubeDeploymentReplicasMismatch` имеет median TTR 29h и p90 = 83h.
В переводе на человеческий: что-то реально сломано в проде
(squad-8-kingdom2, squad-7-shared), но фактический сигнал похоронен под
потоком свежих firing-алёртов. Долгие firing-окна теряются в шуме.

Решение: hourly beat-task `kg_stuck_alerts_check` который:
  1. сканирует `kg_alerts` на firing-окна > MIN_DURATION_HOURS (default 24);
  2. группирует по team_owner (из kg_services.team_owner);
  3. пишет audit-log STUCK_ALERTS_FOUND per team;
  4. опционально шлёт Discord embed в dedicated webhook.

Severity-бамп делается KG-side (только в audit + embed). НЕ трогаем AM —
это отдельный сигнал для оператора, не подмена AlertManager-severity.

Idempotency: 6h dedup window на fingerprint множества stuck-alert-id-ов
(тот же паттерн что в `kg_self_health`). In-memory dedup state живёт
только в worker-процессе — это сознательный trade-off (см. self_health.py).

Read-only по отношению к KG-схеме: ничего не пишем в kg_alerts, только
SELECT-ы + audit-log + Discord.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import structlog
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.knowledge_graph.schema import AlertEvent, Service

log = structlog.get_logger()


# ── Public types ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StuckAlert:
    """Один stuck alert. Плоская dict-репрезентация — стабильный контракт
    для audit-log и render embed."""
    alert_id: int
    alertname: str
    service_name: Optional[str]
    namespace: Optional[str]
    team_owner: Optional[str]
    fired_at: datetime
    hours_firing: float
    severity_current: Optional[str]   # AM-severity (warning/critical/info)
    recurrence_24h: int               # сколько раз тот же alertname для того же
                                      # сервиса firing-нул за последние 24h
    recurrence_7d: int                # …за последние 7d

    def as_dict(self) -> Dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "alertname": self.alertname,
            "service": (
                f"{self.namespace}/{self.service_name}"
                if self.namespace and self.service_name
                else (self.service_name or "—")
            ),
            "namespace": self.namespace,
            "service_name": self.service_name,
            "team_owner": self.team_owner,
            "fired_at": self.fired_at.isoformat(),
            "hours_firing": round(self.hours_firing, 1),
            "severity_current": self.severity_current,
            "recurrence_24h": self.recurrence_24h,
            "recurrence_7d": self.recurrence_7d,
        }


@dataclass
class TeamStuckGroup:
    """Группа stuck-alerts для одной команды."""
    team_owner: str
    alerts: List[StuckAlert] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.alerts)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "team_owner": self.team_owner,
            "count": self.count,
            "alerts": [a.as_dict() for a in self.alerts],
        }


# ── Helpers ───────────────────────────────────────────────────────────────


def _now() -> datetime:
    # Все timestamp'ы в БД — naive UTC (datetime.utcnow). Сравниваем в том
    # же формате, иначе sqlite/postgres падают на tz-comparison.
    return datetime.utcnow()


def _bumped_severity(base: Optional[str], hours_firing: float) -> str:
    """KG-side severity bump.

    Долгое firing-окно эскалирует severity не из-за самого alert-а
    (он может быть warning), а из-за того, что никто его не разрешил.
    Это сигнал «процесс эскалации сломан», а не «сервис критичен».

    Маппинг (дискретный, чтобы не плодить порогов):
      hours >= 48 → "critical"
      hours >= 24 → "high"
      иначе       → исходная severity (нет бампа)
    """
    if hours_firing >= 48:
        return "critical"
    if hours_firing >= 24:
        return "high"
    return base or "warning"


def _recurrence_counts(
    db: Session,
    alertnames: Sequence[str],
    since_24h: datetime,
    since_7d: datetime,
) -> Tuple[Dict[Tuple[int, str], Tuple[int, int]], Dict[str, Tuple[int, int]]]:
    """Recurrence-счётчики ОДНИМ агрегатом на весь прогон.

    Возвращает два индекса:
      * `{(service_id, alertname): (count_24h, count_7d)}` — для алертов с
        привязкой к сервису;
      * `{alertname: (count_24h, count_7d)}` — суммы по всем сервисам, для
        orphan-алертов (service_id IS NULL): у них recurrence считается по
        одному alertname по всем ns, как и было.

    24h-счётчик берём CASE-суммой внутри 7d-окна: 24h ⊂ 7d, отдельный запрос
    не нужен. Скан ограничен `alertnames` залипших алертов — агрегат не
    превращается в проход по всей 7-дневной истории kg_alerts.
    """
    grouped = (
        db.query(
            AlertEvent.service_id,
            AlertEvent.alertname,
            func.count(AlertEvent.id).label("cnt_7d"),
            func.sum(
                case((AlertEvent.fired_at >= since_24h, 1), else_=0)
            ).label("cnt_24h"),
        )
        .filter(
            AlertEvent.alertname.in_(list(alertnames)),
            AlertEvent.fired_at >= since_7d,
        )
        .group_by(AlertEvent.service_id, AlertEvent.alertname)
        .all()
    )

    by_svc: Dict[Tuple[int, str], Tuple[int, int]] = {}
    by_name: Dict[str, Tuple[int, int]] = {}
    for row in grouped:
        c24 = int(row.cnt_24h or 0)
        c7d = int(row.cnt_7d or 0)
        if row.service_id is not None:
            by_svc[(int(row.service_id), row.alertname)] = (c24, c7d)
        name_24h, name_7d = by_name.get(row.alertname, (0, 0))
        by_name[row.alertname] = (name_24h + c24, name_7d + c7d)
    return by_svc, by_name


# ── Core query ────────────────────────────────────────────────────────────


def find_stuck_alerts(
    db: Session,
    min_duration_hours: int = 24,
) -> List[Dict[str, Any]]:
    """Найти alerts firing > min_duration_hours без resolved_at.

    Возвращает список dict-ов (один per alert) с полями:
      alertname, service (ns/name), team_owner,
      fired_at, hours_firing, severity_current,
      recurrence_24h, recurrence_7d.

    LEFT JOIN на kg_services — alert может быть orphan (без service_id),
    в этом случае team_owner=None и попадает в группу "unknown".

    Стоимость: РОВНО 2 запроса независимо от числа залипших алертов (сам
    список + один агрегат recurrence). Раньше на каждый stuck-алерт летели
    2 отдельных COUNT-а — при десятках-сотнях залипших это сотни запросов
    каждый час (задача hourly), причём ровно в те моменты, когда БД и без
    того под инцидентом.
    """
    cutoff = _now() - timedelta(hours=min_duration_hours)
    now = _now()
    since_24h = now - timedelta(hours=24)
    since_7d = now - timedelta(days=7)

    rows = (
        db.query(
            AlertEvent.id,
            AlertEvent.alertname,
            AlertEvent.severity,
            AlertEvent.fired_at,
            AlertEvent.service_id,
            Service.name,
            Service.namespace,
            Service.team_owner,
        )
        .outerjoin(Service, Service.id == AlertEvent.service_id)
        .filter(AlertEvent.fired_at <= cutoff)
        .filter(AlertEvent.resolved_at.is_(None))
        .order_by(AlertEvent.fired_at.asc())
        .all()
    )

    if not rows:
        return []

    stuck_names = {r.alertname for r in rows if r.alertname is not None}
    rec_by_svc, rec_by_name = _recurrence_counts(
        db, sorted(stuck_names), since_24h, since_7d,
    )

    result: List[Dict[str, Any]] = []
    for r in rows:
        hours = (now - r.fired_at).total_seconds() / 3600.0
        # Recurrence считаем по (service_id, alertname). Если service_id
        # None — orphan, recurrence по одному alertname по всем ns
        # (значит и расстановка приоритета будет грубее, но это окей).
        if r.service_id is not None:
            rec_24h, rec_7d = rec_by_svc.get((r.service_id, r.alertname), (0, 0))
        else:
            rec_24h, rec_7d = rec_by_name.get(r.alertname, (0, 0))

        result.append({
            "alert_id": int(r.id),
            "alertname": r.alertname,
            "service": (
                f"{r.namespace}/{r.name}"
                if r.namespace and r.name
                else (r.name or "—")
            ),
            "service_name": r.name,
            "namespace": r.namespace,
            "team_owner": r.team_owner,
            "fired_at": r.fired_at.isoformat(),
            "hours_firing": round(hours, 1),
            "severity_current": r.severity,
            "severity_bumped": _bumped_severity(r.severity, hours),
            "recurrence_24h": rec_24h,
            "recurrence_7d": rec_7d,
        })
    return result


# ── Grouping ──────────────────────────────────────────────────────────────


def group_by_team(stuck: Sequence[Dict[str, Any]]) -> List[TeamStuckGroup]:
    """Группировать flat-список stuck-alerts по team_owner.

    Внутри команды сортируем по hours_firing desc — самые старые сверху.
    Команды сортируем по count desc, затем по name asc для стабильного порядка.
    """
    groups: Dict[str, List[StuckAlert]] = defaultdict(list)
    for s in stuck:
        team = s.get("team_owner") or "unknown"
        groups[team].append(StuckAlert(
            alert_id=s["alert_id"],
            alertname=s["alertname"],
            service_name=s.get("service_name"),
            namespace=s.get("namespace"),
            team_owner=s.get("team_owner"),
            fired_at=datetime.fromisoformat(s["fired_at"]),
            hours_firing=float(s["hours_firing"]),
            severity_current=s.get("severity_current"),
            recurrence_24h=int(s.get("recurrence_24h") or 0),
            recurrence_7d=int(s.get("recurrence_7d") or 0),
        ))

    out: List[TeamStuckGroup] = []
    for team, alerts in groups.items():
        alerts.sort(key=lambda a: a.hours_firing, reverse=True)
        out.append(TeamStuckGroup(team_owner=team, alerts=alerts))
    out.sort(key=lambda g: (-g.count, g.team_owner))
    return out


# ── Fingerprint for dedup ─────────────────────────────────────────────────


def fingerprint(stuck: Sequence[Dict[str, Any]]) -> str:
    """Стабильный fingerprint для dedup-окна.

    Используем sorted-set alert-id-ов: если набор stuck-alert'ов тот же —
    та же ситуация, повторно не алёртим. Когда хоть один stuck резолвится
    или появляется новый — fingerprint меняется и мы шлём свежий embed.

    Возвращает строку (например, "12,17,42") — пустая, если список пуст.
    """
    ids = sorted(int(s["alert_id"]) for s in stuck)
    return ",".join(str(i) for i in ids)


# ── Severity emoji (используется в digest render) ─────────────────────────


def severity_emoji(severity: Optional[str], hours_firing: Optional[float] = None) -> str:
    """Эмодзи для severity с учётом возможного KG-side бампа.

    Если hours_firing задан — используем bumped severity для подбора эмодзи,
    иначе берём как есть. Маппинг намеренно консервативный (не более одного
    эмодзи на уровень):
      critical → 🔴
      high     → 🟠
      warning  → 🟡
      info     → 🔵
      _        → ⚪
    """
    if hours_firing is not None:
        effective = _bumped_severity(severity, hours_firing)
    else:
        effective = (severity or "").lower()
    mapping = {
        "critical": "🔴",
        "high": "🟠",
        "warning": "🟡",
        "info": "🔵",
    }
    return mapping.get(effective.lower(), "⚪")
