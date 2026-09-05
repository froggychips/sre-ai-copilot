"""D2-auto: drift cleanup в auto-режиме.

`run_drift_cleanup(db, max_drift_pct=20.0, apply=True)` — основная функция:

1. Через `kubectl get ns` собирает live-namespace set.
2. Сравнивает с `kg_services.namespace` distinct — БЕЗ синтетических ns
   (см. `_SYNTHETIC_NAMESPACES`): их в кластере нет по построению.
3. Оставляет только те, чьё отсутствие подтверждено вторым, независимым
   наблюдателем: `kg_namespaces.state='missing'` дольше `_MISSING_GRACE`.
   Это и есть защита от kubectl-failure — разовый пустой ответ отметку не
   создаёт, а `namespace_lifecycle` снимает её сразу, как ns вернулся.
4. **Safety threshold**: если live-набор УСОХ относительно активного в графе
   больше чем на max_drift_pct — no-op, `skipped_threshold=True`.
5. Если apply=True — UPDATE kg_services в подтверждённых ns: `synthetic=true`
   + `metadata.drift_marked_at` + `metadata.drift_reason`.

ИСТОРИЯ: порог считался по доле дрейфа от размера графа и потому блокировал
сам себя — доля растёт именно оттого, что чистка не идёт. Перевалив 20%
однажды, чистка выключалась навсегда. Замер 05.09.2026: `drift_pct=39.39`,
`marked_services=0`, 4275 живых записей в 91 снесённом namespace, 2325 из
них попадали в отчёты как `suspicious_stale`. Усадка live-набора отвечает
на тот вопрос, который порог и должен был задавать: «а kubectl вообще
ответил правду?»

Возвращает dict-stats для logging/celery.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Set

from sqlalchemy.orm import Session

from app.knowledge_graph.kubectl_breaker import run_kubectl
from app.knowledge_graph.nats_subjects_sync import NATS_SUBJECTS_NAMESPACE
from app.knowledge_graph.schema import (NS_STATE_ACTIVE, NS_STATE_MISSING,
                                        Namespace, Service)

log = logging.getLogger(__name__)

# Синтетические namespace: чисто KG-конструкции, в k8s их нет по определению
# (`nats-subjects` держит subject-узлы из nats_subjects_sync). В сравнении
# kg_ns vs live-ns они попадали в drift КАЖДЫЙ прогон: subject-узлы получали
# `drift_reason`/`drift_marked_at`, а drift_pct был постоянно завышен.
_SYNTHETIC_NAMESPACES = frozenset({NATS_SUBJECTS_NAMESPACE})

#: Сколько namespace должен числиться `missing` в `kg_namespaces`, прежде чем
#: его сервисы можно помечать. Сутки — заведомо больше любого флапа kubectl
#: (lifecycle ходит каждые 10 минут и снимает отметку сразу же), и заведомо
#: меньше срока, за который снесённый стенд успевает испортить отчёты.
_MISSING_GRACE = timedelta(hours=24)


class DriftCleanupSkipped(Exception):
    """Raised когда safety threshold спасает от accidental mass-mark."""


def _k8s_live_namespaces() -> Set[str]:
    """kubectl get ns → set имён. Raises на kubectl-failure.

    `-o name` вместо jsonpath: jsonpath с `\\n`-разделителями через
    python subprocess может фейлиться на shell-escape. `-o name` даёт
    стабильный формат `namespace/<name>` per line.
    """
    out = run_kubectl(
["kubectl", "get", "ns", "-o", "name"],
timeout=15,
)
    if out.returncode != 0:
        raise RuntimeError(
            f"kubectl get ns failed (rc={out.returncode}): "
            f"{out.stderr.strip()[:200]}"
        )
    return {
        ln.removeprefix("namespace/").strip()
        for ln in out.stdout.splitlines()
        if ln.strip()
    }


def _confirmed_missing(db: Session, grace: timedelta, now: datetime) -> Set[str]:
    """Namespace, отсутствие которых подтверждено lifecycle дольше `grace`.

    `kg_namespaces` ведёт независимое наблюдение: `namespace_lifecycle`
    ставит `state=missing` + `missing_since` и снимает их, как только ns
    появляется снова. Разовый сбой kubectl такую отметку не переживает — она
    снимется на следующем же прогоне, и до конца grace-периода ns сюда не
    попадёт.

    Ns, которого lifecycle не видел вовсе (строки нет), не подтверждён — и
    помечать по нему нечего.
    """
    rows = (
        db.query(Namespace.namespace, Namespace.missing_since)
        .filter(Namespace.state == NS_STATE_MISSING,
                Namespace.missing_since.isnot(None))
        .all()
    )
    return {ns for ns, since in rows if since is not None and now - since >= grace}


def run_drift_cleanup(
    db: Session,
    max_drift_pct: float = 20.0,
    apply: bool = True,
    grace: timedelta = _MISSING_GRACE,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Drift cleanup с safety.

    Помечаются только namespace, отсутствие которых подтверждено дважды:
    их нет в `kubectl get ns` СЕЙЧАС и `kg_namespaces` держит их
    `state=missing` дольше `grace`. Второе условие и есть защита от
    kubectl-failure: одного пустого ответа мало, чтобы lifecycle
    продержал отметку сутки.

    max_drift_pct остался страховкой от абсурда, но считается по УСАДКЕ
    live-набора относительно того, что граф считает живым, а не по доле
    накопленного мусора. Прежняя формулировка блокировала сама себя: доля
    дрейфа растёт ровно потому, что чистка не идёт, и, перевалив порог
    однажды, выключала чистку навсегда. Замер 05.09.2026 —
    `drift_pct=39.39 > 20.00, marked_services=0`: 4275 живых записей в 91
    снесённом namespace, из них 2325 в отчётах как `suspicious_stale`.

    Returns:
        {
          "kg_ns_count": int,
          "k8s_ns_count": int,
          "drift_ns": List[str],          # нет в кластере сейчас
          "unconfirmed_ns": List[str],    # из них ещё не подтверждены lifecycle
          "drift_pct": float,
          "shrink_pct": float,            # усадка live-набора vs active в графе
          "skipped_threshold": bool,
          "marked_services": int,  # 0 если apply=False или skipped
          "applied": bool,
        }
    """
    stamp = now or datetime.now(timezone.utc).replace(tzinfo=None)
    k8s_ns = _k8s_live_namespaces()
    kg_ns = {
        ns for (ns,) in db.query(Service.namespace).distinct().all()
        if ns not in _SYNTHETIC_NAMESPACES
    }
    drift = sorted(kg_ns - k8s_ns)
    confirmed = _confirmed_missing(db, grace, stamp)
    to_mark = sorted(set(drift) & confirmed)

    active_in_graph = (
        db.query(Namespace).filter(Namespace.state == NS_STATE_ACTIVE).count()
    )
    shrink_pct = (
        round(100.0 * max(active_in_graph - len(k8s_ns), 0) / active_in_graph, 2)
        if active_in_graph else 0.0
    )

    stats: Dict[str, Any] = {
        "kg_ns_count": len(kg_ns),
        "k8s_ns_count": len(k8s_ns),
        "drift_ns": drift,
        "unconfirmed_ns": sorted(set(drift) - confirmed),
        "drift_pct": round(100.0 * len(drift) / max(len(kg_ns), 1), 2),
        "shrink_pct": shrink_pct,
        "skipped_threshold": False,
        "marked_services": 0,
        "applied": False,
    }

    if not drift:
        log.info("drift_cleanup.no_drift kg_ns=%d", len(kg_ns))
        return stats

    if shrink_pct > max_drift_pct:
        # kubectl недосчитался живых namespace относительно того, что граф
        # считает активным. Это признак сбоя запроса, а не сноса стендов:
        # столько окружений разом не исчезает.
        stats["skipped_threshold"] = True
        log.warning(
            "drift_cleanup.live_set_shrank shrink_pct=%.2f max=%.2f "
            "k8s_ns=%d active_in_graph=%d",
            shrink_pct, max_drift_pct, len(k8s_ns), active_in_graph,
        )
        return stats

    if not to_mark:
        log.info(
            "drift_cleanup.nothing_confirmed drift=%d unconfirmed=%d grace_h=%.0f",
            len(drift), len(stats["unconfirmed_ns"]),
            grace.total_seconds() / 3600,
        )
        return stats

    drift = to_mark

    if not apply:
        return stats

    # PG: metadata_json — SQLAlchemy.JSON, в Postgres хранится как json.
    # Для merge через `||` нужен cast в jsonb. Используем ORM-loop для
    # idempotent merge — проще чем raw SQL с cast-проблемами.
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    affected = db.query(Service).filter(Service.namespace.in_(drift)).all()
    marked = 0
    for s in affected:
        # Legacy: metadata_json у некоторых старых services хранится как list
        # (например, после устаревших ETL-проходов). Защита от
        # AttributeError: 'list' object has no attribute 'get'.
        existing_meta: Dict[str, Any] = s.metadata_json if isinstance(s.metadata_json, dict) else {}
        if s.synthetic and existing_meta.get("drift_reason"):
            continue  # уже помечен ранее — пропускаем
        s.synthetic = True
        meta = dict(existing_meta)
        meta["drift_marked_at"] = now_iso
        meta["drift_reason"] = "ns_not_in_k8s"
        s.metadata_json = meta
        marked += 1
    db.commit()
    stats["marked_services"] = marked
    stats["applied"] = True
    log.info(
        "drift_cleanup.applied drift_ns=%d marked_services=%d drift_pct=%.2f",
        len(drift), marked, stats["drift_pct"],
    )
    return stats
