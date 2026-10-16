"""Перенести историю алертов с экспортеров метрик на их k8s-target.

До 16.10.2026 store-путь клал алерты, пришедшие через экспортер, на сам
экспортер: `vm-kube-state-metrics` (KubePodCrashLooping, *PodStuckNotReady,
KubeContainerWaiting, KubePdb*, KubeNodePressure…) и `vm-node-exporter`
(Node*). Замер за 30 дней: 168 из 592 алертов, 49 KGIncident на экспортерах.
Новые алерты пишутся правильно (`resolve_store_target`), этот скрипт чинит
накопленное, иначе дайджест (chronic, MTTR) ещё месяц показывал бы экспортеры.

Лейблов в kg_alerts нет — объект берётся из `raw.description`:
  * под → workload по имени пода (`_strip_pod_hash`), если такой узел есть в
    графе в namespace из описания;
  * PDB → узел с именем PDB (как в store-пути), если он есть в графе;
  * нодовые алерты (Node*/KubeNode*/DevNode*, node-exporter) → без сервиса,
    `raw.node`, если имя ноды есть в описании;
  * остальное → без сервиса (unresolved). Экспортер владельцем не остаётся.

Что НЕ делает (ревью Codex):
  * не заводит инциденты для истории — attach склеил бы старый эпизод с
    текущим открытым инцидентом сервиса. Опустевшие инциденты экспортеров
    удаляются. Дайджест (chronic, MTTR) считает по kg_alerts.

Ограничение отката: журнал лежит в `raw`, а повторное уведомление того же
fingerprint (горящий или вновь загоревшийся алерт) перезаписывает `raw`
целиком. Такие строки `--undo` не увидит — `service_id` у них останется
перенесённым. Откат — best-effort; перед `--apply` снять dry-run.

Безопасность:
  * dry-run по умолчанию; `--apply` — запись;
  * журнал отката в строке: `raw.reattributed_from` (прежний service_id,
    incident_id, время). `--undo --apply` возвращает строки по нему;
  * SAVEPOINT на алерт, commit на батч — сбой одной строки не откатывает
    уже перенесённое и не держит блокировки на всю историю;
  * идемпотентно: строка, ушедшая с экспортера, повторно не кандидат.

CLI:
    python -m app.scripts.reattribute_exporter_alerts             # dry-run
    python -m app.scripts.reattribute_exporter_alerts --apply
    python -m app.scripts.reattribute_exporter_alerts --undo [--apply]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Dict, List, Optional, Tuple, cast

from sqlalchemy.orm import Session

from app.core.timeutil import utcnow
from app.database import SessionLocal
from app.knowledge_graph.populator import upsert_service
from app.knowledge_graph.schema import (NODE_KIND_SERVICE, NODE_KIND_WORKLOAD,
                                        AlertEvent, Service)
from app.scripts.reattribute_job_alerts import _detach_from_incident
from app.services.alert_enrichment import (_NODE_ALERT_PREFIXES,
                                           _NODE_EXPORTER_SOURCES,
                                           METRIC_EXPORTER_SOURCES,
                                           _strip_pod_hash)

_BATCH = 200

# Под в описаниях правил (чарт victoria-metrics-k8s-stack и наши VMRule).
_POD_PATTERNS = (
    # KubePodCrashLooping: «Pod <ns>/<pod> (<container>) is in waiting state»
    re.compile(r"\bPod (?P<ns>[a-z0-9-]+)/(?P<pod>[a-z0-9.-]+)\b"),
    # KubeContainerWaiting: «pod/<pod> in namespace <ns> on container …»
    re.compile(r"\bpod/(?P<pod>[a-z0-9.-]+) in namespace (?P<ns>[a-z0-9-]+)"),
    # *PodStuckNotReady: «kubectl -n <ns> describe pod <pod>»
    re.compile(r"-n (?P<ns>[a-z0-9-]+) describe pod (?P<pod>[a-z0-9.-]+)"),
)
_PDB_RE = re.compile(r"\bPDB /?(?P<ns>[a-z0-9-]+)/(?P<pdb>[a-z0-9.-]+)\b")
# Workload прямо в описании — история до KSM-target фикса (май–авг. 2026):
# KubeDeployment{Generation,Replicas}Mismatch, *RolloutStuck,
# KubeStatefulSet*/KubeDaemonSet*, KubeHpaReplicasMismatch (HPA → workload
# по имени: связь scaleTargetRef в графе не хранится, но 16.10.2026 у всех
# 55 HPA кластера имя == scaleTargetRef.name; иначе — проверять target), наши
# PreprodDeploymentNotProgressing. Без этих шаблонов dry-run 16.10.2026 на
# проде оставлял без владельца ~920 строк при известном target.
_WORKLOAD_PATTERNS = (
    re.compile(
        r"\b(?:Deployment|StatefulSet|DaemonSet|HPA)(?: generation for)? "
        r"(?P<ns>[a-z0-9-]+)/(?P<name>[a-z0-9.-]+)\b"
    ),
    re.compile(r"\b[Rr]ollout of deployment (?P<ns>[a-z0-9-]+)/(?P<name>[a-z0-9.-]+)\b"),
    re.compile(r"-n (?P<ns>[a-z0-9-]+) rollout status deploy/(?P<name>[a-z0-9.-]+)"),
)
# Имя ноды, если описание его называет («node dev-6 (», «узле infra-2 (»,
# «dev-ноде dev-28», «dev-28 on cluster»). IP:9100 — адрес пода экспортера,
# не ноды: по нему ноду не восстановить.
_NODE_PATTERNS = (
    re.compile(r"\b(?:node|узле|ноде|на) (?P<node>[a-z][a-z0-9-]*\d[a-z0-9-]*) \("),
    re.compile(r"dev-ноде (?P<node>[a-z][a-z0-9-]*\d)"),
    re.compile(r"^(?P<node>[a-z][a-z0-9-]*\d) on cluster"),
)


def _description(alert: AlertEvent) -> str:
    raw = alert.raw if isinstance(alert.raw, dict) else {}
    return str(raw.get("description") or "")


def _is_node_alert(alertname: str, exporter: str) -> bool:
    return (
        exporter in _NODE_EXPORTER_SOURCES
        or alertname.startswith(_NODE_ALERT_PREFIXES)
        or alertname.startswith("DevNode")
    )


def _node_from(desc: str) -> Optional[str]:
    for rx in _NODE_PATTERNS:
        m = rx.search(desc)
        if m:
            return m.group("node")
    return None


def _graph_has(db: Session, namespace: str, name: str) -> bool:
    return db.query(Service.id).filter(
        Service.namespace == namespace, Service.name == name,
        Service.node_kind.in_((NODE_KIND_SERVICE, NODE_KIND_WORKLOAD)),
    ).first() is not None


def resolve_target(
    db: Session, alertname: str, exporter: str, desc: str,
) -> Tuple[Optional[Tuple[str, str]], str, Optional[str]]:
    """((namespace, service) | None, how, node) для одной строки истории."""
    if _is_node_alert(alertname, exporter):
        return None, "node", _node_from(desc)
    for rx in _POD_PATTERNS:
        m = rx.search(desc)
        if not m:
            continue
        ns, pod = m.group("ns"), m.group("pod")
        derived = _strip_pod_hash(pod)
        if derived and derived not in METRIC_EXPORTER_SOURCES and _graph_has(db, ns, derived):
            return (ns, derived), "pod", None
        return None, "pod_not_in_graph", None
    m = _PDB_RE.search(desc)
    if m and _graph_has(db, m.group("ns"), m.group("pdb")):
        return (m.group("ns"), m.group("pdb")), "pdb", None
    for rx in _WORKLOAD_PATTERNS:
        m = rx.search(desc)
        if not m:
            continue
        ns, name = m.group("ns"), m.group("name")
        if name not in METRIC_EXPORTER_SOURCES and _graph_has(db, ns, name):
            return (ns, name), "workload", None
        return None, "workload_not_in_graph", None
    return None, "no_target", None


def _on_exporters(db: Session) -> Any:
    return (
        db.query(AlertEvent, Service)
        .join(Service, Service.id == AlertEvent.service_id)
        .filter(Service.name.in_(sorted(METRIC_EXPORTER_SOURCES)))
    )


def _candidates(db: Session) -> List[Tuple[AlertEvent, Service]]:
    """Все алерты на экспортерах — и закрытые, и горящие.

    Горящие тоже (ревью Codex): `record_alert_event` при повторе того же
    fingerprint `service_id` не меняет, и пропущенная строка осталась бы на
    экспортере навсегда — новый store-путь её не перепишет.
    """
    return cast(List[Tuple[AlertEvent, Service]], (
        _on_exporters(db).order_by(AlertEvent.fired_at).all()
    ))


def reattribute(db: Session, *, apply: bool = False) -> Dict[str, Any]:
    stats: Dict[str, Any] = {
        "applied": apply, "candidates": 0, "to_service": 0, "to_node": 0,
        "unresolved": 0, "moved": 0, "failed": 0, "incidents_deleted": 0,
        "incidents_shrunk": 0, "by_how": {}, "by_exporter": {}, "top_targets": {},
        "sample": [],
    }
    rows = _candidates(db)
    stats["candidates"] = len(rows)
    # Горящие: их raw перезапишет следующее уведомление — журнал отката для
    # них best-effort (см. шапку).
    stats["firing"] = sum(1 for a, _ in rows if a.resolved_at is None)
    for i, (alert, exporter_svc) in enumerate(rows, 1):
        exporter = cast(str, exporter_svc.name)
        alertname = cast(str, alert.alertname)
        target, how, node = resolve_target(db, alertname, exporter, _description(alert))
        stats["by_how"][how] = stats["by_how"].get(how, 0) + 1
        stats["by_exporter"][exporter] = stats["by_exporter"].get(exporter, 0) + 1
        if target:
            stats["to_service"] += 1
            key = f"{target[0]}/{target[1]}"
            stats["top_targets"][key] = stats["top_targets"].get(key, 0) + 1
        elif how == "node":
            stats["to_node"] += 1
        else:
            stats["unresolved"] += 1
        if len(stats["sample"]) < 10:
            stats["sample"].append(
                f"{alertname} {exporter_svc.namespace}/{exporter} → "
                f"{'/'.join(target) if target else ('node ' + (node or '?')) if how == 'node' else '∅'} ({how})"
            )
        if not apply:
            continue
        try:
            with db.begin_nested():
                _move(db, alert, exporter, target, how, node, stats)
            stats["moved"] += 1
        except Exception as e:  # noqa: BLE001 — одна строка не валит батч
            stats["failed"] += 1
            print(f"[skip] alert {alert.id}: {type(e).__name__}: {e}", file=sys.stderr)
        if i % _BATCH == 0:
            db.commit()
    if apply:
        db.commit()
    stats["top_targets"] = dict(
        sorted(stats["top_targets"].items(), key=lambda kv: -kv[1])[:15]
    )
    return stats


def _move(
    db: Session, alert: AlertEvent, exporter: str,
    target: Optional[Tuple[str, str]], how: str, node: Optional[str],
    stats: Dict[str, Any],
) -> None:
    fate = _detach_from_incident(db, alert)
    if fate == "deleted":
        stats["incidents_deleted"] += 1
    elif fate == "shrunk":
        stats["incidents_shrunk"] += 1
    raw = dict(alert.raw) if isinstance(alert.raw, dict) else {}
    raw["reattributed_from"] = {
        "service_id": alert.service_id, "incident_id": alert.incident_id,
        "at": utcnow().isoformat(), "how": how,
    }
    raw["source"] = exporter
    raw["attribution"] = how
    if node and not raw.get("node"):
        raw["node"] = node
    row: Any = alert
    row.raw = raw  # новый dict: JSON-колонка не отслеживает мутации на месте
    # Инцидент для истории НЕ заводим (ревью Codex): attach_alert склеил бы
    # старый эпизод с текущим открытым инцидентом сервиса или открыл бы новый
    # из закрытого алерта — длительности и MTTR исказились бы. Дайджест
    # (chronic, MTTR) считает по kg_alerts, ему хватает service_id.
    row.incident_id = None
    if target is None:
        row.service_id = None
        db.flush()
        return
    svc = upsert_service(db, namespace=target[0], name=target[1], node_kind=NODE_KIND_SERVICE)
    db.flush()
    row.service_id = svc.id
    db.flush()


def undo(db: Session, *, apply: bool = False) -> Dict[str, Any]:
    """Вернуть строки на экспортер по журналу `raw.reattributed_from`."""
    stats: Dict[str, Any] = {"applied": apply, "candidates": 0, "restored": 0, "failed": 0}
    rows = [a for a in db.query(AlertEvent).all()
            if isinstance(a.raw, dict) and isinstance(a.raw.get("reattributed_from"), dict)]
    stats["candidates"] = len(rows)
    if not apply:
        return stats
    for i, alert in enumerate(rows, 1):
        try:
            with db.begin_nested():
                raw = dict(cast(Dict[str, Any], alert.raw))
                prev = raw.pop("reattributed_from")
                for key in ("source", "attribution"):
                    raw.pop(key, None)
                row: Any = alert
                row.raw = raw
                row.service_id = prev.get("service_id")
                # Откат возвращает атрибуцию, но не членство в инциденте:
                # перенос уже убрал fingerprint из инцидента (или удалил его),
                # и голая ссылка без fingerprints/alert_count рассогласовала
                # бы инцидент (ревью Codex).
                row.incident_id = None
                db.flush()
            stats["restored"] += 1
        except Exception as e:  # noqa: BLE001
            stats["failed"] += 1
            print(f"[skip] alert {alert.id}: {type(e).__name__}: {e}", file=sys.stderr)
        if i % _BATCH == 0:
            db.commit()
    db.commit()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="записать (без флага — dry-run)")
    parser.add_argument("--undo", action="store_true", help="вернуть по raw.reattributed_from")
    args = parser.parse_args()
    db = SessionLocal()
    try:
        result = undo(db, apply=args.apply) if args.undo else reattribute(db, apply=args.apply)
    finally:
        db.close()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not args.apply:
        print("\ndry-run: --apply чтобы записать.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
