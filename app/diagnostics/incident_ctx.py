"""Адаптер Incident + analyzer-output → ctx для DiagnosticEngine.

Один источник правды о том, какие поля попадают в правила.

ВАЖНО (circular fact contamination): вывод AnalyzerAgent — LLM-проза, а не
наблюдение. Раньше он клался в `k8s_summary` и попадал в Rule.text_haystack:
спекуляция анализатора «this may be OOMKilled» превращалась в
Fact(oom_killed, observed=True, conf=0.95), на который затем якорились
hypothesis-агенты и который проходил critic — «детерминированный» слой
фабриковал факты из текста модели. Теперь analyzer-вывод доступен правилам
только как `analyzer_summary` (НЕ входит в text_haystack), а `k8s_summary` /
`logs_summary` зарезервированы под наблюдаемые источники (K8sFacts, логи,
events) — их заполняет pipeline после enrichment-а.
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
             "buildtype_id": "MyProject_BuildServiceX",
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
            "changes": b.get("changes") or [],
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
        analyzer_summary: вывод AnalyzerAgent — кладём ТОЛЬКО в
            `analyzer_summary` (вне Rule.text_haystack): LLM-проза не должна
            фабриковать «наблюдаемые» факты через regex-сканы правил.
        kg_session: опциональная сессия БД. Если передана, попытаемся
            подтянуть nearby_alerts из knowledge_graph для UpstreamDegradedRule.
            Без неё `upstream_alerts` остаётся None и правило сигналит
            «no_graph_data» (это сейчас норма — populator ещё не работает).

    Returns:
        dict с полями: incident, namespace, service, pod, alertname,
        description, analyzer_summary, k8s_summary, recent_deployments,
        metrics_summary, upstream_alerts, incident_starts_at.
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
        # LLM-вывод — ВНЕ text_haystack: правила его не сканируют.
        "analyzer_summary": analyzer_summary,
        # Наблюдаемые текстовые источники. Заполняются pipeline-ом после
        # enrichment-а: k8s_summary/logs_summary ← K8sFacts.collect_snapshot().
        "k8s_summary": None,
        "logs_summary": None,
        "recent_deployments": _extract_deploys_from_tc(incident.teamcity_context),
        "metrics_summary": None,  # TODO: brать из ContextBuilder.metrics
        "upstream_alerts": upstream_alerts,
        "incident_starts_at": incident_starts_at,
    }
