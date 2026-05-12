"""Адаптер Incident + analyzer-output → ctx для DiagnosticEngine.

Один источник правды о том, какие поля попадают в правила. Если правило
полагается на «k8s_summary», смотри здесь — оно собирается из analyzer-вывода.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.knowledge_graph.queries import nearby_alerts
from app.models.incident import Incident


def _extract_deploys_from_tc(tc_ctx: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """teamcity_context.recent_builds → формат, который понимает RecentDeployRule.

    teamcity_service возвращает что-то вроде:
        {
          "branch": "master",
          "recent_builds": [
            {"number": "1234", "status": "SUCCESS",
             "buildtype_id": "Wo_Backend_K8sNewCluster_ServiceX",
             "branch": "master", "finished_at": "2026-05-12T09:30:00Z"},
            ...
          ]
        }
    Мы превращаем `finished_at` → `ts`, чтобы RecentDeployRule мог
    однообразно сравнивать времена.
    """
    if not tc_ctx:
        return []
    builds = tc_ctx.get("recent_builds") or []
    out: List[Dict[str, Any]] = []
    for b in builds:
        ts = b.get("finished_at") or b.get("started_at") or b.get("at")
        if ts is None:
            continue
        out.append({
            "name": b.get("buildtype_id") or b.get("name", "tc-build"),
            "ts": ts,
            "sha": b.get("sha"),
            "repo": b.get("repo"),
            "buildtype_id": b.get("buildtype_id"),
            "number": b.get("number"),
            "status": b.get("status"),
            "branch": b.get("branch"),
        })
    return out


def _parse_ts(raw: Any) -> Optional[datetime]:
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def build_diagnostics_ctx(
    incident: Incident,
    analyzer_summary: str,
    kg_session: Optional[Session] = None,
) -> Dict[str, Any]:
    """Собрать enriched ctx для DiagnosticEngine.

    Args:
        incident: исходный alert, уже сматченный с TeamCity context.
        analyzer_summary: вывод AnalyzerAgent — кладём в k8s_summary, чтобы
            правила могли regex-сканировать.
        kg_session: опциональная сессия БД. Если передана, попытаемся
            подтянуть nearby_alerts из knowledge_graph для UpstreamDegradedRule.
            Без неё `upstream_alerts` остаётся None и правило сигналит
            «no_graph_data» (это сейчас норма — populator ещё не работает).

    Returns:
        dict с полями: incident, namespace, service, pod, alertname,
        description, k8s_summary, recent_deployments, metrics_summary,
        upstream_alerts, incident_starts_at.
    """
    labels = incident.labels or {}
    annotations = incident.annotations or {}

    incident_starts_at = _parse_ts(incident.starts_at)

    upstream_alerts: Optional[List[Dict[str, Any]]] = None
    if kg_session is not None and incident.namespace and labels.get("service"):
        if incident_starts_at is not None:
            try:
                upstream_alerts = nearby_alerts(
                    kg_session,
                    namespace=incident.namespace,
                    service_name=labels["service"],
                    around=incident_starts_at,
                    window_minutes=15,
                )
            except Exception:
                # Граф недоступен / повреждён — не валим pipeline.
                upstream_alerts = None

    return {
        "incident": incident.model_dump(),
        "namespace": incident.namespace,
        "service": labels.get("service"),
        "pod": labels.get("pod"),
        "alertname": labels.get("alertname", ""),
        "description": annotations.get("description") or incident.description or "",
        "k8s_summary": analyzer_summary,
        "logs_summary": None,  # TODO: брать из ContextBuilder если будет интегрирован
        "recent_deployments": _extract_deploys_from_tc(incident.teamcity_context),
        "metrics_summary": None,  # TODO: brать из ContextBuilder.metrics
        "upstream_alerts": upstream_alerts,
        "incident_starts_at": incident_starts_at,
    }
