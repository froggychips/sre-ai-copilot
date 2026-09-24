"""Компактный снимок контекста инцидента — то, что видели правила и агенты.

Зачем. До 1.0.20 инцидент хранил вердикты (факты, гипотезы, итог) и статусы
источников (`source_coverage`), но не сами данные, на которых они выросли.
Live-RCA датасет (`scripts/live_rca_dataset.py`) на реальных инцидентах
получал на вход один алерт — и модель честно отказывалась: 2 из 2 кейсов
пилота 24.09.2026, Top-1 = 0 не из-за модели, а из-за пустого входа. Снимок
закрывает эту дыру: через пару недель у новых инцидентов будет настоящий вход,
и датасет начнёт мерить качество разбора, а не долю отказов.

Только то, чего в графе НЕТ. События подов (`kg_pod_events`), алерты
(`kg_alerts`) и деплои (`kg_deployments`) граф хранит сам, с историей, и их
можно поднять point-in-time по окну (так и делает `live_rca_dataset.py
export`, метка `context=kg_reconstructed`). Копировать их в analysis — значит
держать вторую, отстающую версию тех же строк. Вместо копий — `kg_refs`:
namespace/service/pod и окно, по которым строки находятся, плюс сколько их
видел пайплайн.

Что внутри (`schema: incident_ctx/v1`):
  alert           — alertname / namespace / service / pod / метки / описание;
  facts           — FactStore: kind, verdict, confidence, сжатый evidence;
  source_status   — Known Unknowns как есть: без них снимок врёт так же, как
                    правило без source_status (пусто ≠ «ничего не было»);
  k8s_pod_state   — живой снимок K8sFacts на момент диагноза: pod → reason /
                    exit_code / message (terminated / lastState). В графе его
                    нет — синк видит события, а не состояние контейнера;
  metrics_summary, cluster_health — живые значения VM на момент диагноза;
  rollout_suppressed, core_dump_node — выводы enrichment-а;
  logs_summary    — хвост текстового blob-а K8sFacts (он же уходит в LLM);
  kg_refs         — ссылки на строки графа вместо их копий.

LLM-вывод (`analyzer_summary`) сюда намеренно НЕ входит: снимок — вход для
разбора, а не его результат; с прозой модели датасет мерил бы сам себя.

Два жёстких ограничения:
  * PII/секреты — каждая строка проходит `redact_pii` (тот же фильтр, что у
    pod-логов в LLM-контексте): снимок лежит в `incidents.analysis`, его
    читают timeline, отчёты и выгрузка датасета;
  * размер — не больше `MAX_SNAPSHOT_BYTES` сериализованного JSON. Лишнее
    срезается по приоритету (сначала логи, потом события, соседние алерты…),
    срезанное перечислено в `truncated`, чтобы потребитель видел, что снимок
    неполный, а не решил, что «событий не было».
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from app.services.pii_redaction import redact_pii

SNAPSHOT_SCHEMA = "incident_ctx/v1"
MAX_SNAPSHOT_BYTES = 32 * 1024

# Потолки на уровне отдельных значений — до общей подгонки размера.
_STR_MAX = 300
_DESCRIPTION_MAX = 1000
_LOGS_MAX = 8000
_LIST_MAX = 20
_DICT_MAX = 40
_DEPTH_MAX = 4

# Метки алерта, которые несут смысл для разбора. Остальные (prometheus,
# receiver, внутренние метки оператора) — шум, раздувающий снимок.
_ALERT_LABELS = (
    "alertname", "severity", "namespace", "service", "pod", "container",
    "deployment", "statefulset", "job", "job_name", "node", "instance",
    "reason", "phase",
)

# Порядок подгонки размера: что срезаем первым. Логи — самое толстое и
# самое заменимое (события и pod_state несут ту же причину в сжатом виде).
# alert / facts / source_status не режутся никогда — без них снимок пуст.
_TRIM_ORDER = (
    "logs_summary",
    "cluster_health",
    "metrics_summary",
    "k8s_pod_state",
)

# Окно, по которому строки графа поднимаются задним числом: то же, что берёт
# реконструкция датасета (события за 2 часа до алерта; деплои — за 6).
_KG_EVENTS_BEFORE_MIN = 120
_KG_DEPLOYS_BEFORE_MIN = 360


def _text(value: Any, limit: int = _STR_MAX) -> str:
    """Строка → без PII и не длиннее `limit` (с маркером обрезки)."""
    s = redact_pii(str(value), max_len=None)
    if len(s) > limit:
        return s[: max(limit - 1, 0)] + "…"
    return s


def _compact(value: Any, depth: int = 0) -> Any:
    """Рекурсивно сжать JSON-подобное значение: строки — через redact,
    списки/словари — с потолками, глубина — ограничена. Неизвестные типы
    превращаются в строку: снимок обязан сериализоваться всегда."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, str):
        return _text(value)
    if depth >= _DEPTH_MAX:
        return _text(json.dumps(value, default=str, ensure_ascii=False))
    if isinstance(value, dict):
        items = list(value.items())
        out = {str(k): _compact(v, depth + 1) for k, v in items[:_DICT_MAX]}
        if len(items) > _DICT_MAX:
            out["…"] = f"+{len(items) - _DICT_MAX} ключей"
        return out
    if isinstance(value, (list, tuple, set)):
        seq = list(value)
        out_list = [_compact(v, depth + 1) for v in seq[:_LIST_MAX]]
        if len(seq) > _LIST_MAX:
            out_list.append(f"… +{len(seq) - _LIST_MAX}")
        return out_list
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _compact(to_dict(), depth)
        except Exception:
            pass
    return _text(value)


def _alert_section(ctx: Dict[str, Any]) -> Dict[str, Any]:
    incident = ctx.get("incident") or {}
    labels = incident.get("labels") if isinstance(incident, dict) else None
    labels = labels if isinstance(labels, dict) else {}
    starts = ctx.get("incident_starts_at") or (
        incident.get("starts_at") if isinstance(incident, dict) else None
    )
    return {
        "alertname": ctx.get("alertname") or labels.get("alertname") or "",
        "namespace": ctx.get("namespace"),
        "service": ctx.get("service"),
        "pod": ctx.get("pod"),
        "starts_at": _compact(starts),
        "labels": {k: _text(labels[k]) for k in _ALERT_LABELS if labels.get(k)},
        "description": _text(ctx.get("description") or "", _DESCRIPTION_MAX),
    }


def _facts_section(facts: Any) -> List[Dict[str, Any]]:
    if facts is None:
        return []
    items = getattr(facts, "facts", None)
    items = items() if callable(items) else items
    out: List[Dict[str, Any]] = []
    for f in items or []:
        out.append({
            "kind": getattr(f, "kind", None),
            "verdict": getattr(f, "verdict", None),
            "confidence": getattr(f, "confidence", None),
            "subject": getattr(f, "subject", None),
            "source_rule": getattr(f, "source_rule", None),
            "unknown_reason": getattr(f, "unknown_reason", None),
            "evidence": _compact(getattr(f, "evidence", None) or {}, depth=2),
        })
    return out


def _kg_refs(ctx: Dict[str, Any], alert: Dict[str, Any]) -> Dict[str, Any]:
    """Ссылки на строки графа, которые видел пайплайн, — без самих строк."""
    def _count(key: str) -> Optional[int]:
        v = ctx.get(key)
        return len(v) if isinstance(v, (list, tuple)) else None

    corr = ctx.get("deploy_correlation")
    return {
        "namespace": alert.get("namespace"),
        "service": alert.get("service"),
        "pod": alert.get("pod"),
        "anchor": alert.get("starts_at"),
        "pod_events": {"table": "kg_pod_events", "before_min": _KG_EVENTS_BEFORE_MIN,
                       "seen": _count("k8s_events")},
        "deployments": {"table": "kg_deployments", "before_min": _KG_DEPLOYS_BEFORE_MIN,
                        "seen": _count("recent_deployments")},
        "upstream_alerts": {"table": "kg_alerts", "seen": _count("upstream_alerts")},
        # Вывод корреляции — не копия строк, а решение enrichment-а: храним
        # сам вывод (deploy/сервис/время), но не выборку метрик под ним.
        "deploy_correlation": _compact(
            {k: corr.get(k) for k in ("deploy", "reason", "verdict", "confidence", "time_proximity_minutes",
                                      "n_spikes", "max_zscore") if k in corr}
        ) if isinstance(corr, dict) else None,
    }


def _size(obj: Dict[str, Any]) -> int:
    return len(json.dumps(obj, default=str, ensure_ascii=False).encode("utf-8"))


def build_context_snapshot(
    ctx: Optional[Dict[str, Any]],
    facts: Any = None,
    *,
    max_bytes: int = MAX_SNAPSHOT_BYTES,
) -> Dict[str, Any]:
    """Собрать снимок из ctx правил (`build_diagnostics_ctx` + enrichment)
    и FactStore. Никогда не бросает: снимок — best-effort побочный продукт,
    сбой его сборки не должен ронять пайплайн."""
    ctx = ctx or {}
    alert = _alert_section(ctx)
    snap: Dict[str, Any] = {
        "schema": SNAPSHOT_SCHEMA,
        "alert": alert,
        "facts": _facts_section(facts),
        "source_status": _compact(ctx.get("source_status") or {}),
        "k8s_pod_state": _compact(ctx.get("k8s_pod_state") or {}),
        "kg_refs": _kg_refs(ctx, alert),
        "metrics_summary": _compact(ctx.get("metrics_summary")),
        "cluster_health": _compact(ctx.get("cluster_health")),
        "rollout_suppressed": _compact(ctx.get("rollout_suppressed")),
        "core_dump_node": _compact(ctx.get("core_dump_node")),
        "logs_summary": None,
        "truncated": [],
    }
    logs = ctx.get("logs_summary")
    if logs:
        text = redact_pii(str(logs), max_len=None)
        # Хвост, а не голова: причина падения — в последних строках лога.
        if len(text) > _LOGS_MAX:
            text = "…" + text[-(_LOGS_MAX - 1):]
            snap["truncated"].append("logs_summary:tail")
        snap["logs_summary"] = text

    for key in _TRIM_ORDER:
        if _size(snap) <= max_bytes:
            break
        value = snap.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list) and len(value) > 5:
            snap[key] = value[:5]
            snap["truncated"].append(f"{key}:first5")
            if _size(snap) <= max_bytes:
                break
        snap[key] = None
        snap["truncated"].append(key)

    if _size(snap) > max_bytes:
        # Последний рубеж: evidence фактов — самое толстое в неприкасаемых
        # секциях. Вердикты остаются, их размер ограничен числом правил.
        for f in snap["facts"]:
            f["evidence"] = None
        snap["truncated"].append("facts:evidence")
    snap["bytes"] = _size(snap)
    return snap
