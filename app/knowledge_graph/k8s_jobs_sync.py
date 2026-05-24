"""KG Coverage #1: k8s Jobs + CronJobs → KG.

До сих пор KG покрывал только Deployments и StatefulSets (через `kg_sync`
+ `k8s_topology_resources_sync`). Job / CronJob оставались невидимы — а
это:

* alembic migration jobs (DB schema rollout) — если failed, copilot не
  знает, что прод stuck на старой схеме;
* backup CronJobs (etcd snapshot, postgres dumps, push-s3) — если cron
  не запускался N дней или валится с exit-code != 0, никто не услышит;
* ad-hoc cleanup / reindex / cdn-prewarm Jobs — те же blind-spots.

Этот модуль закрывает gap:

* **Jobs** (`batch/v1 Job`):
  - upsert в новую таблицу `kg_k8s_jobs` с полями
    `succeeded_count` / `failed_count` / `active_count` /
    `completion_time` / `last_pod_exit_code` (из podStatus последнего
    Pod с label-selector `job-name=<name>`).

* **CronJobs** (`batch/v1 CronJob`):
  - upsert в ту же таблицу с `kind='cronjob'` и полями `schedule` /
    `last_schedule_time` / `last_successful_time` / `suspended`.
  - edge `runs_as_job` (ServiceEdge): CronJob → owner Service из
    `kg_services`. Match через label `app.kubernetes.io/part-of` или
    `app` совпадающий с `kg_services.name` в том же namespace. Если
    matched нет — edge не создаётся (без bloat).

Sync дёргается kubectl-ом (см. `k8s_topology_resources_sync` для
паттерна — единая зависимость, нет нужды в kubernetes-client). Ошибки
не raise: failure в одном tick'е не должна валить beat-loop.

CLI:
    python -m app.knowledge_graph.k8s_jobs_sync             # все ns
    python -m app.knowledge_graph.k8s_jobs_sync prod-shared
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, cast

from sqlalchemy.orm import Session

from app.knowledge_graph.schema import K8sJob, Service

logger = logging.getLogger(__name__)

_KUBECTL_TIMEOUT_S = 30

# Edge "runs_as_job" реализован не через kg_service_edges (там оба конца —
# Service-узлы), а через `owner_service_id` в metadata_json у CronJob/Job
# узла. Семантика та же: CronJob owned by Service. Это compromise: ради
# одного edge-типа выделять отдельный poly-graph не оправдано.
DISCOVERED_BY_JOB = "k8s_jobs_sync/job"
DISCOVERED_BY_CRONJOB = "k8s_jobs_sync/cronjob"

# Label-ключи, по которым CronJob/Job атрибутируется к owner Service.
# Порядок — приоритет: part-of самый канонический (k8s recommended label),
# `app` — fallback на legacy chart'ы. Можно расширить (`app.kubernetes.io/name`)
# но пока two-level хватает; больше label'ов → шире false-match risk.
_OWNER_LABEL_KEYS = ("app.kubernetes.io/part-of", "app")


# ── kubectl wrappers ────────────────────────────────────────────────────────


def _kubectl_get_all(resource: str) -> List[Dict[str, Any]]:
    """`kubectl get <resource> -A -o json` → items list (или [] при ошибке).

    Те же принципы что в k8s_topology_resources_sync: timeout-safe,
    не raise, лог в warning. Отдельный helper здесь, а не shared, чтобы
    не вводить cross-module зависимость на helper приватного API —
    эти sync-модули должны быть максимально независимы.
    """
    try:
        out = subprocess.run(
            ["kubectl", "get", resource, "-A", "-o", "json"],
            capture_output=True, text=True, check=False,
            timeout=_KUBECTL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        logger.warning("k8s_jobs_sync.kubectl_timeout resource=%s", resource)
        return []
    except Exception as e:
        logger.warning(
            "k8s_jobs_sync.kubectl_exception resource=%s err=%s",
            resource, e,
        )
        return []

    if out.returncode != 0:
        logger.warning(
            "k8s_jobs_sync.kubectl_failed resource=%s rc=%d stderr=%s",
            resource, out.returncode, (out.stderr or "").strip()[:200],
        )
        return []

    try:
        data = json.loads(out.stdout or "{}")
    except json.JSONDecodeError as e:
        logger.warning(
            "k8s_jobs_sync.json_decode_failed resource=%s err=%s",
            resource, e,
        )
        return []

    items = data.get("items") or []
    return items if isinstance(items, list) else []


def _kubectl_get_pod_exit_code(namespace: str, job_name: str) -> Optional[int]:
    """Вытащить exit-code из последнего failed-pod у Job.

    `kubectl get pod -n <ns> -l job-name=<name> -o jsonpath=...` — самый
    быстрый путь без kubernetes-client. Берём именно terminated.exitCode
    у первого containerStatus.

    Returns None если:
      * нет pod с label-selector (Job ещё не запускал ни одного pod);
      * podStatus.containerStatuses нет (init/pending);
      * jsonpath выкинул пусто.

    Намеренно используем `-o jsonpath` а не json+parse: один shell-вызов,
    без 2-3 KB JSON-парсинга per-job. На 200+ jobs в cluster это economy.
    """
    try:
        out = subprocess.run(
            [
                "kubectl", "get", "pod",
                "-n", namespace,
                "-l", f"job-name={job_name}",
                "--sort-by=.status.startTime",
                "-o",
                "jsonpath={.items[-1].status.containerStatuses[0].state.terminated.exitCode}",
            ],
            capture_output=True, text=True, check=False,
            timeout=_KUBECTL_TIMEOUT_S,
        )
    except Exception as e:
        logger.warning(
            "k8s_jobs_sync.pod_exit_code_failed ns=%s job=%s err=%s",
            namespace, job_name, e,
        )
        return None

    if out.returncode != 0:
        return None

    raw = (out.stdout or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


# ── pure helpers ────────────────────────────────────────────────────────────


def _parse_k8s_time(ts: Optional[str]) -> Optional[datetime]:
    """k8s timestamps — ISO 8601 c `Z`. Возвращаем naive UTC (как везде в KG)."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


def _extract_job_status(job: Dict[str, Any]) -> Dict[str, Any]:
    """Job.status → плоский dict для upsert.

    Counts: k8s гарантирует int >= 0, но field может отсутствовать на
    свежесозданном Job — нормализуем в 0.
    """
    status = job.get("status") or {}
    return {
        "succeeded_count": int(status.get("succeeded") or 0),
        "failed_count": int(status.get("failed") or 0),
        "active_count": int(status.get("active") or 0),
        "start_time": _parse_k8s_time(status.get("startTime")),
        "completion_time": _parse_k8s_time(status.get("completionTime")),
    }


def _extract_cronjob_status(cj: Dict[str, Any]) -> Dict[str, Any]:
    """CronJob.spec.schedule + CronJob.status → плоский dict.

    `suspend` — bool в spec; True если cron намеренно остановлен.
    Различение «suspended» vs «никогда не запускался» важно для
    health-сигналов (suspended != down).
    """
    spec = cj.get("spec") or {}
    status = cj.get("status") or {}
    return {
        "schedule": spec.get("schedule"),
        "suspended": bool(spec.get("suspend") or False),
        "last_schedule_time": _parse_k8s_time(status.get("lastScheduleTime")),
        "last_successful_time": _parse_k8s_time(status.get("lastSuccessfulTime")),
        "active_count": len(status.get("active") or []),
    }


def _extract_pod_template_labels(obj: Dict[str, Any], kind: str) -> Dict[str, str]:
    """Достать labels pod-template из Job или CronJob.

    Job: `spec.template.metadata.labels`
    CronJob: `spec.jobTemplate.spec.template.metadata.labels`

    Возвращаем dict (может быть пустой). Используется для owner-attribution.
    """
    spec = obj.get("spec") or {}
    if kind == "cronjob":
        template = ((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}
    else:
        template = spec.get("template") or {}
    return ((template.get("metadata") or {}).get("labels")) or {}


def _resolve_owner_service_name(
    obj_labels: Dict[str, str],
    pod_labels: Dict[str, str],
) -> Optional[str]:
    """Найти owner service name по labels (proritized).

    Сначала смотрим labels самого Job/CronJob, потом pod-template labels —
    helm-чарты часто кладут `app.kubernetes.io/part-of` именно на pod, а не
    на job metadata.

    Возвращает первое не-None значение для известных ключей.
    """
    for src in (obj_labels, pod_labels):
        for key in _OWNER_LABEL_KEYS:
            val = src.get(key)
            if val:
                return val
    return None


# ── upsert ──────────────────────────────────────────────────────────────────


def _upsert_k8s_job(
    db: Session,
    *,
    namespace: str,
    name: str,
    kind: str,
    fields: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
) -> K8sJob:
    """Idempotent upsert по (namespace, name, kind).

    Несколько раз в неделю Job-ы пересоздаются (`apply -f migrate-job.yaml`
    с тем же name). В таком случае k8s сохраняет name → дубля не будет;
    counters сбрасываются на новые значения, что и нужно.

    CronJob-ы переименовываются редко — но если name тот же, а
    `lastSuccessfulTime` сдвинулся — мы корректно обновим.
    """
    existing = (
        db.query(K8sJob)
        .filter(
            K8sJob.namespace == namespace,
            K8sJob.name == name,
            K8sJob.kind == kind,
        )
        .one_or_none()
    )
    now = datetime.utcnow()
    if existing is None:
        node = K8sJob(
            namespace=namespace,
            name=name,
            kind=kind,
            metadata_json=metadata,
            last_seen_at=now,
            **fields,
        )
        db.add(node)
        db.flush()
        return node

    for k, v in fields.items():
        if hasattr(existing, k):
            setattr(existing, k, v)
    if metadata is not None:
        existing.metadata_json = cast(Any, metadata)
    existing.last_seen_at = cast(Any, now)
    db.flush()
    return existing


# ── sync logic ──────────────────────────────────────────────────────────────


def sync_all_jobs(db: Session) -> Dict[str, int]:
    """Sync все Job-ы cluster-wide.

    Returns stats:
        jobs_fetched
        nodes_upserted
        exit_codes_resolved   — сколько Failed Job-ов получили exit_code
    """
    jobs = _kubectl_get_all("jobs")
    stats = {
        "jobs_fetched": len(jobs),
        "nodes_upserted": 0,
        "exit_codes_resolved": 0,
    }

    for job in jobs:
        meta = job.get("metadata") or {}
        ns = meta.get("namespace") or "default"
        name = meta.get("name")
        if not name:
            continue

        status_fields = _extract_job_status(job)

        # Resolve exit-code только когда failed_count > 0 — для succeeded
        # job-а exit=0 уже implied и читать pod зря. Также экономит
        # kubectl-вызовы на 100% success cluster-ах.
        exit_code: Optional[int] = None
        if status_fields["failed_count"] > 0:
            exit_code = _kubectl_get_pod_exit_code(ns, name)
            if exit_code is not None:
                stats["exit_codes_resolved"] += 1

        obj_labels = meta.get("labels") or {}
        pod_labels = _extract_pod_template_labels(job, "job")
        owner_name = _resolve_owner_service_name(obj_labels, pod_labels)

        metadata_json = {
            "labels": obj_labels,
            "owner_label": owner_name,
            # Job чаще всего создаётся CronJob-ом, ownerReferences даёт parent.
            "owner_references": [
                {"kind": r.get("kind"), "name": r.get("name")}
                for r in (meta.get("ownerReferences") or [])
            ],
        }

        fields = dict(status_fields)
        fields["last_pod_exit_code"] = exit_code
        fields["owner_service_name"] = owner_name

        _upsert_k8s_job(
            db,
            namespace=ns,
            name=name,
            kind="job",
            fields=fields,
            metadata=metadata_json,
        )
        stats["nodes_upserted"] += 1

    db.commit()
    logger.info(
        "k8s_jobs_sync.jobs_done fetched=%d nodes=%d exit_codes=%d",
        stats["jobs_fetched"], stats["nodes_upserted"], stats["exit_codes_resolved"],
    )
    return stats


def sync_all_cronjobs(db: Session) -> Dict[str, int]:
    """Sync все CronJob-ы cluster-wide + edge runs_as_job на owner Service.

    Returns stats:
        cronjobs_fetched
        nodes_upserted
        edges_runs_as_job        — сколько runs_as_job edges создано/обновлено
        skipped_no_owner_label   — нет owner label вообще
        skipped_no_owner_match   — label есть, но в kg_services нет matching service
    """
    cronjobs = _kubectl_get_all("cronjobs")
    stats = {
        "cronjobs_fetched": len(cronjobs),
        "nodes_upserted": 0,
        "edges_runs_as_job": 0,
        "skipped_no_owner_label": 0,
        "skipped_no_owner_match": 0,
    }

    for cj in cronjobs:
        meta = cj.get("metadata") or {}
        ns = meta.get("namespace") or "default"
        name = meta.get("name")
        if not name:
            continue

        cron_fields = _extract_cronjob_status(cj)
        obj_labels = meta.get("labels") or {}
        pod_labels = _extract_pod_template_labels(cj, "cronjob")
        owner_name = _resolve_owner_service_name(obj_labels, pod_labels)

        metadata_json = {
            "labels": obj_labels,
            "owner_label": owner_name,
            "concurrency_policy": (cj.get("spec") or {}).get("concurrencyPolicy"),
        }

        fields = dict(cron_fields)
        fields["owner_service_name"] = owner_name

        cj_node = _upsert_k8s_job(
            db,
            namespace=ns,
            name=name,
            kind="cronjob",
            fields=fields,
            metadata=metadata_json,
        )
        stats["nodes_upserted"] += 1

        # Edge runs_as_job: CronJob → owner Service из kg_services.
        # ServiceEdge ожидает Service в обоих концах, поэтому src — это
        # synthetic-service-обёртка над CronJob? Нет — мы хотим связать
        # CronJob (kg_k8s_jobs) с реальным kg_services. Используем
        # отдельный edge-механизм: store в metadata_json.
        if not owner_name:
            stats["skipped_no_owner_label"] += 1
            continue

        owner_svc = (
            db.query(Service)
            .filter_by(namespace=ns, name=owner_name)
            .one_or_none()
        )
        if owner_svc is None:
            stats["skipped_no_owner_match"] += 1
            continue

        # Связь от owner Service → CronJob node храним в metadata_json
        # CronJob-а (owner_service_id). Это semantic «runs_as_job»: owner
        # Service _имеет_ CronJob как побочный workflow.
        meta_with_link: Dict[str, Any] = dict(cj_node.metadata_json or {})
        meta_with_link["owner_service_id"] = owner_svc.id
        cj_node.metadata_json = cast(Any, meta_with_link)
        db.flush()
        stats["edges_runs_as_job"] += 1

    db.commit()
    logger.info(
        "k8s_jobs_sync.cronjobs_done fetched=%d nodes=%d edges=%d "
        "skipped_no_label=%d skipped_no_match=%d",
        stats["cronjobs_fetched"], stats["nodes_upserted"],
        stats["edges_runs_as_job"],
        stats["skipped_no_owner_label"], stats["skipped_no_owner_match"],
    )
    return stats


def _link_jobs_to_cronjob_owners(db: Session) -> int:
    """После sync_all_jobs / sync_all_cronjobs: пройти Job-ы и проставить
    `owner_service_id` если parent CronJob уже имеет такую привязку.

    Это даёт transitive linkage: failed-Job (созданный CronJob-ом) сам
    знает к какому Service относится без повторного label-resolve.

    Возвращает кол-во linked Job-ов.
    """
    linked = 0
    jobs = (
        db.query(K8sJob)
        .filter(K8sJob.kind == "job")
        .all()
    )
    for j in jobs:
        meta: Dict[str, Any] = cast(Dict[str, Any], j.metadata_json or {})
        owner_refs = meta.get("owner_references") or []
        cj_name = None
        for r in owner_refs:
            if (r or {}).get("kind") == "CronJob":
                cj_name = r.get("name")
                break
        if not cj_name:
            continue
        parent = (
            db.query(K8sJob)
            .filter_by(namespace=j.namespace, name=cj_name, kind="cronjob")
            .one_or_none()
        )
        if parent is None:
            continue
        parent_meta: Dict[str, Any] = cast(Dict[str, Any], parent.metadata_json or {})
        owner_id = parent_meta.get("owner_service_id")
        if not owner_id:
            continue
        meta_updated = dict(meta)
        if meta_updated.get("owner_service_id") == owner_id:
            continue
        meta_updated["owner_service_id"] = owner_id
        j.metadata_json = cast(Any, meta_updated)
        linked += 1
    if linked:
        db.commit()
    return linked


def sync_k8s_jobs(db: Session) -> Dict[str, Any]:
    """Main entry: fetch + upsert обе resource-категории.

    CronJob-ы первыми чтобы Job-ы могли проследовать линк через
    ownerReferences→CronJob.owner_service_id (transitive linkage).
    """
    cj_stats = sync_all_cronjobs(db)
    job_stats = sync_all_jobs(db)
    transitive_linked = _link_jobs_to_cronjob_owners(db)
    return {
        "cronjobs": cj_stats,
        "jobs": job_stats,
        "transitive_linked": transitive_linked,
    }


if __name__ == "__main__":
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        result = sync_k8s_jobs(db)
        print(json.dumps(result, indent=2, default=str))
    finally:
        db.close()
    _ = sys.argv  # noqa: SIM107 (placeholder для будущего per-ns CLI)
