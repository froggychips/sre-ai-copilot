"""Deterministic KG-enrichment для AlertManager-алертов.

Собирает контекст из knowledge_graph (recent_deploys, nearby_alerts,
incidents_on, downstream count, team_owner) и прогоняет правила из
app.diagnostics.rules — БЕЗ LLM-вызовов. Используется в
/webhooks/alertmanager/enrich-and-forward.

Структура `EnrichedContext` — то, что builder в discord_service
консьюмит для построения embed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import structlog
from sqlalchemy.orm import Session

from app.config import settings
from app.diagnostics.facts import Fact
from app.diagnostics.rules.recent_deploy import RecentDeployRule
from app.diagnostics.rules.upstream_degraded import UpstreamDegradedRule
from app.knowledge_graph.queries import (incidents_on, nearby_alerts,
                                         recent_deploys_for,
                                         recent_pod_events_for, upstream_of)
from app.knowledge_graph.schema import Service, ServiceEdge
from app.models.incident import Incident

log = structlog.get_logger()


@dataclass
class EnrichedContext:
    """Готовая структура для рисовалки Discord-embed.

    Все поля могут быть пустыми/None — builder обязан это переносить
    (KG может быть холодным или сервис вне graph).
    """

    incident: Incident
    service: Optional[str] = None
    pod: Optional[str] = None
    team_owner: Optional[str] = None
    in_kg: bool = False

    recent_deploys: List[Dict[str, Any]] = field(default_factory=list)
    upstream_alerts: List[Dict[str, Any]] = field(default_factory=list)
    recurrence_24h: List[Dict[str, Any]] = field(default_factory=list)
    # Inbound: сколько сервисов вызывают/зависят от этого. Раньше поле
    # называлось `downstream_count_by_kind` — это была семантическая ошибка
    # (граф: src→dst, edges с dst=svc означают "кто меня вызывает" =
    # inbound callers, не downstream).
    inbound_count_by_kind: Dict[str, int] = field(default_factory=dict)
    # Outgoing: куда сервис сам ходит (edges с src=svc). Это «зависимости» —
    # для leaf-сервисов это самая важная диагностика при падении.
    outgoing_deps: List[Dict[str, Any]] = field(default_factory=list)
    # Pod-events (kg_pod_events) — k8s diagnostic signal в окне инцидента.
    pod_events: List[Dict[str, Any]] = field(default_factory=list)

    rule_facts: List[Fact] = field(default_factory=list)

    # rollout-noise — выставляется heuristic-ом в enrich_alert ниже
    rollout_noise: bool = False

    kg_data_age_sec: Optional[int] = None

    def primary_hypothesis(self) -> Optional[str]:
        """Берёт самый сильный observed fact для подсказки в Root Cause."""
        observed = [f for f in self.rule_facts if f.observed]
        if not observed:
            return None
        # Сортируем по confidence — берём top-1.
        top = max(observed, key=lambda f: f.confidence)
        return _fact_to_short_text(top)


def _fact_to_short_text(fact: Fact) -> str:
    ev = fact.evidence or {}
    if fact.source_rule == "RecentDeployRule":
        deploys = ev.get("deploys") or []
        if deploys:
            d = deploys[0]
            mins = d.get("minutes_before_incident", "?")
            sha = (d.get("sha") or "")[:7]
            build = d.get("number") or d.get("buildtype_id") or "?"
            return f"Deploy #{build} ({sha}) {mins} мин назад — возможный регресс"
    if fact.source_rule == "UpstreamDegradedRule":
        cnt = ev.get("count", 0)
        alerts = ev.get("alerts") or []
        if alerts:
            first = alerts[0]
            svc = first.get("service") or "?"
            an = first.get("alertname") or "?"
            return f"Upstream `{svc}` алертит `{an}` ({cnt} cascading)"
    return f"{fact.source_rule}: observed"


def _parse_starts_at(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str):
        s = raw.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _downstream_count_by_kind(db: Session, namespace: str, service_name: str) -> Dict[str, int]:
    """Сколько сервисов имеют edge → данный (calls / uses_nats и т.д.).

    Делается одним запросом по kg_service_edges, группируется в Python.
    """
    svc = (
        db.query(Service)
        .filter(Service.namespace == namespace, Service.name == service_name)
        .one_or_none()
    )
    if svc is None:
        return {}
    rows = db.query(ServiceEdge).filter(ServiceEdge.dst_id == svc.id).all()
    out: Dict[str, int] = {}
    for r in rows:
        out[r.kind] = out.get(r.kind, 0) + 1
    return out


def _kg_data_age(db: Session, namespace: str, service_name: str) -> Optional[int]:
    svc = (
        db.query(Service)
        .filter(Service.namespace == namespace, Service.name == service_name)
        .one_or_none()
    )
    if svc is None or svc.updated_at is None:
        return None
    age = datetime.now(timezone.utc) - svc.updated_at.replace(tzinfo=timezone.utc)
    return int(age.total_seconds())


def _detect_rollout_noise(incident: Incident, recent_deploys: List[Dict[str, Any]]) -> bool:
    """Heuristic: KubeDeploymentGenerationMismatch + deploy <5 мин назад → noise."""
    alertname = incident.labels.get("alertname", "")
    if alertname not in {"KubeDeploymentGenerationMismatch", "KubeReplicaSetMismatch"}:
        return False
    for d in recent_deploys:
        if d.get("minutes_before_incident", 999) <= 5:
            return True
    return False


def enrich_alert(db: Session, incident: Incident) -> EnrichedContext:
    """Главная точка — синхронный, без LLM, ~5 SQL-запросов.

    Безопасно при пустом KG — каждое поле fallback в пустое значение.
    """
    namespace = incident.namespace or incident.labels.get("namespace", "")
    service = incident.labels.get("service") or incident.labels.get("deployment")
    pod = incident.labels.get("pod")

    ctx = EnrichedContext(
        incident=incident,
        service=service,
        pod=pod,
    )

    if not namespace or not service:
        log.debug("enrich.skip_no_service", namespace=namespace, service=service)
        return ctx

    incident_at = _parse_starts_at(incident.starts_at)

    # 1. Recent deploys: сначала узкое окно (для regression-сигнала
    # «deploy за N минут до alert-а»), если пусто — расширяем до 7 дней,
    # чтобы embed всё равно показал последние deploys (для редко-катящихся
    # сервисов 60-мин окно почти всегда пустое).
    try:
        ctx.recent_deploys = recent_deploys_for(
            db, namespace, service, before=incident_at,
            lookback_minutes=settings.ENRICH_DEPLOY_LOOKBACK_MIN,
        )
        if not ctx.recent_deploys:
            ctx.recent_deploys = recent_deploys_for(
                db, namespace, service, before=incident_at,
                lookback_minutes=7 * 24 * 60,  # 7 дней fallback
            )
    except Exception as e:
        log.warning("enrich.recent_deploys_failed", error=str(e))

    # 2. Upstream alerts (±15 мин)
    try:
        ctx.upstream_alerts = nearby_alerts(
            db, namespace, service, around=incident_at,
            window_minutes=settings.ENRICH_UPSTREAM_WINDOW_MIN,
        )
    except Exception as e:
        log.warning("enrich.nearby_alerts_failed", error=str(e))

    # 3. Recurrence (24h окно)
    try:
        ctx.recurrence_24h = incidents_on(
            db, namespace, service,
            since=incident_at - timedelta(minutes=settings.ENRICH_RECURRENCE_LOOKBACK_MIN),
            until=incident_at,
        )
    except Exception as e:
        log.warning("enrich.incidents_on_failed", error=str(e))

    # 4. Inbound: кто вызывает этот сервис (по kind).
    try:
        ctx.inbound_count_by_kind = _downstream_count_by_kind(db, namespace, service)
    except Exception as e:
        log.warning("enrich.inbound_count_failed", error=str(e))

    # 4b. Outgoing dependencies: куда сервис сам ходит. Для leaf-сервисов
    # это главная диагностика «упал — потому что зависит от X». Fresh-only
    # 30 дней — отсекает stale edges (см. C1 last_seen_at).
    try:
        ctx.outgoing_deps = upstream_of(
            db, namespace, service, fresh_only_days=30,
        )
    except Exception as e:
        log.warning("enrich.outgoing_deps_failed", error=str(e))

    # 4c. Recent pod events (kg_pod_events) — k8s diagnostic signal
    # в окне инцидента ±60 мин. Особенно важно для CrashLooping/Unhealthy,
    # где AM rule показывает следствие, а pod_events — причину
    # (OOMKilled/ImagePullBackOff/FailedScheduling/etc.).
    try:
        ctx.pod_events = recent_pod_events_for(
            db, namespace, service, around=incident_at,
            window_minutes=60, limit=5,
        )
    except Exception as e:
        log.warning("enrich.pod_events_failed", error=str(e))

    # 5. Service metadata (team_owner, in_kg flag, data freshness)
    try:
        svc = (
            db.query(Service)
            .filter(Service.namespace == namespace, Service.name == service)
            .one_or_none()
        )
        if svc is not None:
            ctx.in_kg = True
            ctx.team_owner = svc.team_owner
            if svc.updated_at is not None:
                age = datetime.now(timezone.utc) - svc.updated_at.replace(tzinfo=timezone.utc)
                ctx.kg_data_age_sec = int(age.total_seconds())
    except Exception as e:
        log.warning("enrich.service_lookup_failed", error=str(e))

    # 6. Rule-based hypotheses — без LLM. Передаём в их интерфейс
    #    `recent_deployments` и `upstream_alerts`, как ожидают rules.
    rule_ctx: Dict[str, Any] = {
        "incident": incident.model_dump(),
        "namespace": namespace,
        "service": service,
        "pod": pod,
        "alertname": incident.labels.get("alertname", ""),
        "description": incident.description or "",
        "recent_deployments": ctx.recent_deploys,
        "upstream_alerts": ctx.upstream_alerts if ctx.upstream_alerts else None,
        "incident_starts_at": incident_at,
    }
    try:
        ctx.rule_facts.extend(RecentDeployRule().evaluate(rule_ctx))
    except Exception as e:
        log.warning("enrich.recent_deploy_rule_failed", error=str(e))
    try:
        ctx.rule_facts.extend(UpstreamDegradedRule().evaluate(rule_ctx))
    except Exception as e:
        log.warning("enrich.upstream_rule_failed", error=str(e))

    # 7. Rollout-noise heuristic — `KubeDeploymentGenerationMismatch` сразу
    # после деплоя обычно безобиден (rollout в процессе).
    ctx.rollout_noise = _detect_rollout_noise(incident, ctx.recent_deploys)

    return ctx
