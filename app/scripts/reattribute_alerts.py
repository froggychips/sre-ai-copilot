"""Переатрибуция алертов за N дней цепочкой `alert_target.resolve_alert_target`.

Новые алерты пишутся новой цепочкой; этот скрипт приводит к ней историю
окна сверки, иначе еженедельная сверка ещё неделю показывала бы старую долю
«без атрибуции».

ОТКУДА МЕТКИ. В kg_alerts меток нет (только raw.description/node/source).
Полные метки берутся из серии `ALERTS` VictoriaMetrics за окно и
сопоставляются со строкой по fingerprint Alertmanager-а (FNV-64a по
отсортированным меткам без `alertstate`) — тот же fingerprint, что лежит в
`kg_alerts.fingerprint`. Замер 31.10.2026: совпали 480 из 480 строк недели.
Строка без серии (ретеншн VM короче окна) не трогается.

ЧТО ПЕРЕНОСИТСЯ. Только строки, которые сверка сейчас считает «без
атрибуции» (без сервиса или на экспортере/synthetic), и только если новая
цепочка даёт объект: сервис, ноду, namespace или «алерт про сам экспортер».
Уже атрибутированные строки не трогаются. Объект, чей узел в графе сейчас
synthetic (стенд снесён после алерта), переносится тоже — он верный, но
сверка по-прежнему считает его «на synthetic» (в отчёте — `left … synthetic`).

Безопасность (как у reattribute_exporter_alerts):
  * dry-run по умолчанию; `--apply` — запись;
  * журнал в строке: `raw.reattributed_from` (прежний service_id,
    incident_id, время, how);
  * инцидент медика (на него ссылаются kg_remediation_events) не трогаем —
    меняется только service_id; остальные снимаются с инцидента (пустой
    удаляется), новый инцидент для истории не заводится;
  * SAVEPOINT на строку, commit на батч.

CLI:
    python -m app.scripts.reattribute_alerts                   # dry-run, 7 дней
    python -m app.scripts.reattribute_alerts --days 14
    python -m app.scripts.reattribute_alerts --series-file alerts.json
    python -m app.scripts.reattribute_alerts --apply
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional, cast

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.timeutil import utcnow
from app.knowledge_graph.schema import (NODE_KIND_SERVICE, NODE_KIND_WORKLOAD,
                                        AlertEvent, Service)
from app.services.alert_target import (TARGET_EXPORTER, TARGET_LEGACY,
                                       TARGET_UNRESOLVED,
                                       UNATTRIBUTED_CLASSES, AlertTarget,
                                       DbLookup, TargetLookup,
                                       classify_alert_row,
                                       resolve_alert_target)

_BATCH = 200
_EXCLUDED_SEVERITY = "none"
_SERIES_DROP = frozenset({"__name__", "alertstate"})


def am_fingerprint(labels: Dict[str, str]) -> str:
    """Fingerprint Alertmanager-а (`model.LabelSet.Fingerprint`): FNV-64a по
    отсортированным `имя 0xff значение 0xff`, hex из 16 знаков."""
    h = 0xcbf29ce484222325
    for name in sorted(labels):
        for b in name.encode() + b"\xff" + str(labels[name]).encode() + b"\xff":
            h ^= b
            h = (h * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def labels_by_fingerprint(series: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """{fingerprint: метки алерта} из ответа `/api/v1/series?match[]=ALERTS`."""
    out: Dict[str, Dict[str, str]] = {}
    for s in series:
        labels = {k: str(v) for k, v in s.items() if k not in _SERIES_DROP}
        out.setdefault(am_fingerprint(labels), labels)
    return out


def fetch_alert_series(vm_url: str, days: int, timeout: float = 60.0) -> List[Dict[str, Any]]:
    """Серии ALERTS{alertstate="firing"} за `days` дней из VictoriaMetrics."""
    import httpx
    now = int(time.time())
    resp = httpx.get(
        f"{vm_url.rstrip('/')}/api/v1/series",
        params={"match[]": 'ALERTS{alertstate="firing"}',
                "start": now - days * 86400, "end": now},
        timeout=timeout,
    )
    resp.raise_for_status()
    return cast(List[Dict[str, Any]], resp.json().get("data") or [])


@dataclass
class Row:
    alert: AlertEvent
    service_name: Optional[str]
    service_ns: Optional[str]
    synthetic: bool


def _rows(db: Session, days: int) -> List[Row]:
    since = utcnow().replace(tzinfo=None) - timedelta(days=days)
    q = (
        db.query(AlertEvent, Service.name, Service.namespace, Service.synthetic)
        .outerjoin(Service, Service.id == AlertEvent.service_id)
        .filter(or_(AlertEvent.fired_at >= since, AlertEvent.last_notified_at >= since))
        .filter(or_(AlertEvent.severity.is_(None), AlertEvent.severity != _EXCLUDED_SEVERITY))
        .order_by(AlertEvent.fired_at)
    )
    return [
        Row(a, cast(Optional[str], n), cast(Optional[str], ns), bool(syn))
        for a, n, ns, syn in q.all()
    ]


def _real_workload(db: Session, namespace: Optional[str], name: Optional[str]) -> bool:
    if not namespace or not name:
        return False
    return db.query(Service.id).filter(
        Service.namespace == namespace, Service.name == name,
        Service.node_kind == NODE_KIND_WORKLOAD, Service.synthetic.is_(False),
    ).first() is not None


def _synthetic(db: Session, namespace: Optional[str], name: str) -> bool:
    """Флаг узла, на который upsert_service положит алерт (node_kind=service)."""
    row = db.query(Service.synthetic).filter(
        Service.namespace == namespace, Service.name == name,
        Service.node_kind == NODE_KIND_SERVICE,
    ).first()
    return bool(row[0]) if row is not None else False


def _target_labels(alert: AlertEvent, labels: Dict[str, str]) -> Dict[str, str]:
    out = dict(labels)
    raw = alert.raw if isinstance(alert.raw, dict) else {}
    # Имя ноды store-путь мог дописать сам (node_resolver), в ALERTS его нет.
    if raw.get("node") and not out.get("node"):
        out["node"] = str(raw["node"])
    return out


def _new_class(db: Session, target: AlertTarget, namespace: Optional[str]) -> str:
    if target.service:
        return classify_alert_row(
            target.service, _synthetic(db, namespace, target.service),
            {"target_kind": target.scope},
            real_workload=_real_workload(db, namespace, target.service),
        )
    return classify_alert_row(None, None, {"target_kind": target.scope})


def reattribute(
    db: Session,
    series: Iterable[Dict[str, Any]],
    *,
    days: int = 7,
    apply: bool = False,
    lookup: Optional[TargetLookup] = None,
) -> Dict[str, Any]:
    by_fp = labels_by_fingerprint(series)
    graph = lookup if lookup is not None else DbLookup(db)
    stats: Dict[str, Any] = {
        "applied": apply, "days": days, "total": 0, "no_labels": 0,
        "before": {}, "after": {}, "moved": 0, "failed": 0,
        "incidents_deleted": 0, "incidents_shrunk": 0,
        "by_how": {}, "left": {}, "sample": [],
    }
    rows = _rows(db, days)
    stats["total"] = len(rows)
    for i, row in enumerate(rows, 1):
        alert = row.alert
        before = classify_alert_row(
            row.service_name, row.synthetic, alert.raw,
            real_workload=_real_workload(db, row.service_ns, row.service_name),
        )
        _inc(stats["before"], before)
        labels = by_fp.get(cast(str, alert.fingerprint or ""))
        if labels is None or before not in UNATTRIBUTED_CLASSES:
            if labels is None:
                stats["no_labels"] += 1
            _inc(stats["after"], before)
            continue
        labels = _target_labels(alert, labels)
        target = resolve_alert_target(
            labels,
            legacy_default=labels.get("service") or labels.get("app") or labels.get("deployment"),
            db=db, lookup=graph,
        )
        namespace = target.namespace or labels.get("namespace") or row.service_ns
        after = _new_class(db, target, namespace)
        # Переносим всё, где цепочка нашла объект, — даже если узел цели
        # сейчас synthetic (стенд снесён после алерта): объект верный, а
        # сверка честно покажет его как «на synthetic».
        same = target.service is not None and (namespace, target.service) == (
            row.service_ns, row.service_name)
        # Тот же узел, но алерт про сам экспортер (CrashLoop пода
        # nats-exporter): сервис верный, не хватает только пометки.
        mark_only = same and target.scope == TARGET_EXPORTER
        movable = target.scope not in (TARGET_LEGACY, TARGET_UNRESOLVED) and (
            not same or mark_only)
        if not movable:
            _inc(stats["after"], before)
            _inc(stats["left"], f"{alert.alertname} [{target.scope}/{target.how}]")
            continue
        if after in UNATTRIBUTED_CLASSES:
            _inc(stats["left"], f"{alert.alertname} [{target.scope}/{target.how}, synthetic]")
        _inc(stats["after"], after)
        _inc(stats["by_how"], f"{target.scope}/{target.how}")
        if len(stats["sample"]) < 12:
            stats["sample"].append(
                f"{alert.alertname}: {row.service_name or '∅'} → "
                f"{(namespace or '') + '/' + target.service if target.service else target.scope}"
                f" ({target.how})"
            )
        if not apply:
            continue
        try:
            with db.begin_nested():
                fate = _move(db, alert, target, namespace, mark_only=mark_only)
            stats["moved"] += 1
            if fate == "deleted":
                stats["incidents_deleted"] += 1
            elif fate == "shrunk":
                stats["incidents_shrunk"] += 1
        except Exception as e:  # noqa: BLE001 — одна строка не валит батч
            stats["failed"] += 1
            print(f"[skip] alert {alert.id}: {type(e).__name__}: {e}", file=sys.stderr)
        if i % _BATCH == 0:
            db.commit()
    if apply:
        db.commit()
    stats["left"] = dict(sorted(stats["left"].items(), key=lambda kv: -kv[1])[:15])
    return stats


def _inc(d: Dict[str, int], key: str) -> None:
    d[key] = d.get(key, 0) + 1


def _move(
    db: Session, alert: AlertEvent, target: AlertTarget, namespace: Optional[str],
    *, mark_only: bool = False,
) -> Optional[str]:
    from app.knowledge_graph.populator import upsert_service
    from app.scripts.reattribute_exporter_alerts import _medic_linked
    from app.scripts.reattribute_job_alerts import _detach_from_incident

    # Сервис не меняется — инцидент тоже остаётся (он того же сервиса).
    keep_incident = mark_only or _medic_linked(db, alert.incident_id)
    fate = None if keep_incident else _detach_from_incident(db, alert)
    raw = dict(alert.raw) if isinstance(alert.raw, dict) else {}
    raw["reattributed_from"] = {
        "service_id": alert.service_id, "incident_id": alert.incident_id,
        "at": utcnow().isoformat(), "how": target.how,
    }
    raw["target_kind"] = target.scope
    raw["attribution"] = target.how
    if target.source:
        raw["source"] = target.source
    if target.node and not raw.get("node"):
        raw["node"] = target.node
    if target.service is None and namespace and not raw.get("namespace"):
        raw["namespace"] = namespace
    row: Any = alert
    row.raw = raw  # новый dict: JSON-колонка не отслеживает мутации на месте
    if mark_only:
        db.flush()
        return None
    if not keep_incident:
        row.incident_id = None
    if target.service is None or not namespace:
        row.service_id = None
        db.flush()
        return fate
    svc = upsert_service(db, namespace=namespace, name=target.service, node_kind=NODE_KIND_SERVICE)
    db.flush()
    row.service_id = svc.id
    db.flush()
    return fate


def _shares(counts: Dict[str, int], total: int) -> str:
    bad = sum(v for k, v in counts.items() if k in UNATTRIBUTED_CLASSES)
    parts = ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
    pct = 100.0 * bad / total if total else 0.0
    return f"без атрибуции {bad} из {total} = {pct:.1f}% ({parts})"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    parser.add_argument("--apply", action="store_true", help="записать (по умолчанию dry-run)")
    parser.add_argument("--days", type=int, default=7, help="окно, дней (по умолчанию 7)")
    parser.add_argument("--series-file", help="ответ /api/v1/series ALERTS (JSON) вместо похода в VM")
    args = parser.parse_args(argv)

    if args.series_file:
        with open(args.series_file, encoding="utf-8") as f:
            payload = json.load(f)
        series = payload.get("data", payload) if isinstance(payload, dict) else payload
    else:
        from app.config import settings
        series = fetch_alert_series(settings.VICTORIA_METRICS_URL, args.days)

    from app.database import SessionLocal
    db = SessionLocal()
    try:
        result = reattribute(db, series, days=args.days, apply=args.apply)
    finally:
        db.close()
    total = int(result["total"])
    print(f"было:  {_shares(result['before'], total)}")
    print(f"стало: {_shares(result['after'], total)}")
    print(json.dumps(
        {k: v for k, v in result.items() if k not in ("before", "after")},
        ensure_ascii=False, indent=2, default=str,
    ))
    if not args.apply:
        print("\ndry-run: --apply чтобы перенести.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
