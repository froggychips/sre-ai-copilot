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
import re
import subprocess
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, cast

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.knowledge_graph.schema import NODE_KIND_SERVICE, K8sJob, Service

logger = logging.getLogger(__name__)

_KUBECTL_TIMEOUT_S = 30

# Коммитим порциями, а не одной транзакцией на весь тик — та же дисциплина,
# что в k8s_topology_resources_sync: транзакция держит локи на kg_k8s_jobs /
# kg_services всё время своей жизни, и при волне джоб (накат миграций) это
# минуты, в которые DDL не может взять ACCESS EXCLUSIVE. Синк идемпотентен,
# поэтому потеря атомарности тика безопасна, а долгая блокировка — нет.
_COMMIT_BATCH = 200

# Bounded cleanup для kg_k8s_jobs: строки, не подтверждённые sync-ом дольше
# N часов (default ниже, конфигурируется KG_K8S_JOBS_STALE_HOURS) — удалённые
# из кластера Job/CronJob. Без чистки удалённый one-off migration-Job с
# failed_count>0 жил вечно и продолжал попадать в RCA-корреляции, а
# _link_jobs_to_cronjob_owners гонял все накопленные строки каждые 30 мин.
_STALE_JOBS_MAX_AGE_HOURS_DEFAULT = 48
# Threshold-cap (та же дисциплина, что drift_cleanup / edge-decay): удаление
# > этого % строк kind-а — симптом сбоя kubectl, а не реальной убыли.
_STALE_JOBS_MAX_DELETE_PCT = 25.0

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

# ── Name-pattern fallback resolver ──────────────────────────────────────────
#
# Большинство Job/CronJob в кластере не имеют canonical `app.kubernetes.io/
# part-of` label — особенно те, что созданы ручным `kubectl apply` или
# helm-чарты, написанные «как принято в команде». Однако имена следуют
# устойчивому паттерну: `<service>-backup`, `<service>-cron`,
# `<service>-migration-<timestamp>` и т.п.
#
# Прод-recon 2026-05-25 показал linkage = 5/63 = 7.94%. Большинство
# unlinked — `*-backup`, `*-cleanup`, `*-migration-*`. Этот fallback
# восстанавливает связь, не требуя trip к owner-команде.
#
# Каждый regex должен:
#   * якоривать конец строки (`$`), чтобы случайный `-backupcheck` не
#     цеплялся за `-backup$`;
#   * захватывать сам suffix в одну named group `suffix`, чтобы strip
#     был тривиален через `name[:-len(match.group())]`;
#   * НЕ хватать service-name в нулевую группу (используем строковое
#     отрезание suffix-а — стабильнее и быстрее).
#
# Order не важен: матчится первый, любой matching кандидат строится
# одинаково. Но мы держим _NAME_SUFFIX_PATTERNS как tuple для
# детерминированной диагностики.
_NAME_SUFFIX_PATTERNS: Tuple[re.Pattern[str], ...] = (
    # `foo-backup`, `foo-backup-20240101120000`, `foo-backup-2024-01-01`,
    # `foo-backup-abc123`. CronJob создаёт Job с auto-suffix (unix-ts,
    # ISO-date или 5-char hash). Принимаем alphanumerics + `-` внутри
    # auto-suffix, потому что k8s name pattern это допускает.
    re.compile(r"-backup(?:-[a-z0-9-]+)?$"),
    # `foo-cron`, `foo-cronjob`.
    re.compile(r"-cron(?:job)?$"),
    # `foo-migration`, `foo-migration-20240101`, `foo-migration-2024-01-01`,
    # `foo-migration-abc12`. Alembic / ad-hoc DB migrations почти всегда
    # такого вида.
    re.compile(r"-migration(?:-[a-z0-9-]+)?$"),
    # `foo-migrate`, `foo-migrate-1716542400`, `foo-migrate-2024-06-01`.
    re.compile(r"-migrate(?:-[a-z0-9-]+)?$"),
    # `foo-init`, `foo-init-job`.
    re.compile(r"-init(?:-job)?$"),
    # `foo-cleanup`, `foo-cleanup-20240101`, `foo-cleanup-2024-01-01`.
    re.compile(r"-cleanup(?:-[a-z0-9-]+)?$"),
    # `foo-restore`, `foo-restore-from-snap`.
    re.compile(r"-restore(?:-[a-z0-9-]+)?$"),
    # `foo-reindex`, `foo-reindex-20240101`, `foo-reindex-2024-01-01`.
    re.compile(r"-reindex(?:-[a-z0-9-]+)?$"),
)

# Provenance values для metadata_json.owner_resolved_via.
# Используется для debug + quality_report (распределение источников).
_RESOLVED_VIA_PART_OF = "part-of_label"
_RESOLVED_VIA_APP = "app_label"
_RESOLVED_VIA_NAME_PATTERN = "name_pattern"
_RESOLVED_VIA_NONE = "none"


def _strip_name_suffix(name: str) -> Optional[str]:
    """Strip известный jobs-suffix → predicted service name, либо None.

    Идёт по `_NAME_SUFFIX_PATTERNS` и возвращает имя без первого matching
    suffix-а. Сам поиск — `re.search` (НЕ `fullmatch`), потому что мы
    хотим найти suffix как hвостовую часть, а не сматчить всю строку.

    Edge-кейсы:
      * пустое имя → None;
      * имя совпадает с suffix-ом без префикса (`-backup` целиком) → None,
        иначе получили бы пустой service name;
      * имя без любого matching suffix → None — линковать нечего.
    """
    if not name:
        return None
    for pat in _NAME_SUFFIX_PATTERNS:
        m = pat.search(name)
        if not m:
            continue
        candidate = name[: m.start()]
        if not candidate:
            # `-backup` без префикса — patalogical, не служба.
            return None
        return candidate
    return None


def _resolve_owner_via_name_pattern(
    db: Session, namespace: str, name: str,
) -> Optional[Tuple[str, int]]:
    """Найти owner Service по name-pattern fallback'у.

    Стрипаем известный suffix (`-backup`, `-migration`, …) и проверяем
    наличие kg_services row с этим именем в _том же_ namespace. Cross-NS
    matching намеренно ЗАПРЕЩЁН: backup-job в `prod-shared` не должен
    указывать на сервис из `dev-shared` (false-positive risk).

    Returns (service_name, service_id) или None.
    """
    candidate = _strip_name_suffix(name)
    if not candidate:
        return None
    svc = (
        db.query(Service)
        .filter_by(namespace=namespace, name=candidate, node_kind=NODE_KIND_SERVICE)
        .one_or_none()
    )
    if svc is None:
        return None
    return candidate, cast(int, svc.id)


def _resolve_owner(
    db: Session,
    *,
    namespace: str,
    name: str,
    obj_labels: Dict[str, str],
    pod_labels: Dict[str, str],
) -> Tuple[Optional[str], Optional[int], str]:
    """Унифицированный owner-resolver с provenance.

    Приоритет:
      1. label `app.kubernetes.io/part-of` (canonical k8s)
      2. label `app` (legacy charts)
      3. name-pattern fallback (heuristic, ns-local)
      4. none

    Returns (owner_name, owner_service_id, resolved_via).
    owner_name None ↔ resolved_via == 'none'.
    owner_service_id может быть None даже когда owner_name найден (label
    указывает на сервис, которого нет в kg_services — kept for backward
    compat с текущим pipeline'ом, sync ещё может пометить skip).
    """
    # Label-based: смотрим metadata Job/CronJob, потом pod-template.
    for src in (obj_labels, pod_labels):
        if not src:
            continue
        if src.get("app.kubernetes.io/part-of"):
            return src["app.kubernetes.io/part-of"], None, _RESOLVED_VIA_PART_OF
        if src.get("app"):
            return src["app"], None, _RESOLVED_VIA_APP

    # Name-pattern fallback: ходит в БД, поэтому только после label-checks.
    pat_match = _resolve_owner_via_name_pattern(db, namespace, name)
    if pat_match is not None:
        owner_name, owner_id = pat_match
        return owner_name, owner_id, _RESOLVED_VIA_NAME_PATTERN

    return None, None, _RESOLVED_VIA_NONE


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
        # SAVEPOINT: параллельный beat-tick мог вставить ту же строку между
        # нашим one_or_none() и flush() → INSERT упрётся в UNIQUE
        # (uq_kg_k8s_job_ns_name_kind), и без savepoint IntegrityError
        # переведёт Session в aborted-состояние, теряя весь tick. begin_nested
        # откатывает только этот INSERT; затем перечитываем строку-победителя и
        # апдейтим её (existing-путь ниже). Зеркалит per-item SAVEPOINT из
        # k8s_events_sync.
        try:
            with db.begin_nested():
                db.add(node)
                db.flush()
            return node
        except IntegrityError:
            existing = (
                db.query(K8sJob)
                .filter(
                    K8sJob.namespace == namespace,
                    K8sJob.name == name,
                    K8sJob.kind == kind,
                )
                .one()
            )
            # проваливаемся в update-путь ниже (last-write-wins).

    for k, v in fields.items():
        if hasattr(existing, k):
            setattr(existing, k, v)
    if metadata is not None:
        existing.metadata_json = cast(Any, metadata)
    existing.last_seen_at = cast(Any, now)
    db.flush()
    return existing


# ── sync logic ──────────────────────────────────────────────────────────────


def _prefetch_failed_job_exit_codes(
    jobs: List[Dict[str, Any]], stats: Dict[str, int],
) -> Dict[Tuple[str, str], int]:
    """kubectl-фетчи exit-кодов failed-джоб — ДО открытия транзакции тика.

    Раньше `_kubectl_get_pod_exit_code` вызывался из `_sync_one_job`, то есть
    внутри уже открытой транзакции: волна failed-джоб (накат миграций) давала
    N внешних вызовов по 30с каждый, пока транзакция держала локи на
    kg_k8s_jobs / kg_services. Ровно эту дисциплину («внешние вызовы раньше
    первого SQL») чинили в k8s_topology_resources_sync и kg_sync.

    Ключ — (namespace, name), дубли пар фетчим один раз. Фильтр по
    failed_count>0 сохранён: у succeeded job-а exit=0 уже implied, и на 100%
    success кластере kubectl не вызывается вовсе.

    Разбор status обёрнут в try: битую запись разберёт основной цикл под
    SAVEPOINT-ом и посчитает в `errors` — префетч не вправе ронять тик.
    """
    resolved: Dict[Tuple[str, str], int] = {}
    for job in jobs:
        try:
            meta = job.get("metadata") or {}
            ns = meta.get("namespace") or "default"
            name = meta.get("name")
            if not name:
                continue
            if _extract_job_status(job)["failed_count"] <= 0:
                continue
            key = (ns, name)
            if key in resolved:
                continue
            code = _kubectl_get_pod_exit_code(ns, name)
        except Exception as e:
            logger.warning("k8s_jobs_sync.exit_code_prefetch_failed err=%s", e)
            continue
        if code is not None:
            resolved[key] = code
            stats["exit_codes_resolved"] += 1
    return resolved


def _sync_one_job(
    db: Session,
    job: Dict[str, Any],
    stats: Dict[str, int],
    exit_codes: Optional[Dict[Tuple[str, str], int]] = None,
) -> None:
    """Обработать один Job. Вызывается под per-item SAVEPOINT-ом.

    `exit_codes` — карта из `_prefetch_failed_job_exit_codes`, собранная ДО
    транзакции. None (прямой вызов из CLI/тестов) → старое поведение с
    kubectl по месту.
    """
    meta = job.get("metadata") or {}
    ns = meta.get("namespace") or "default"
    name = meta.get("name")
    if not name:
        return

    status_fields = _extract_job_status(job)

    # Resolve exit-code только когда failed_count > 0 — для succeeded
    # job-а exit=0 уже implied и читать pod зря. Также экономит
    # kubectl-вызовы на 100% success cluster-ах.
    exit_code: Optional[int] = None
    if status_fields["failed_count"] > 0:
        if exit_codes is not None:
            exit_code = exit_codes.get((ns, name))
        else:
            exit_code = _kubectl_get_pod_exit_code(ns, name)
            if exit_code is not None:
                stats["exit_codes_resolved"] += 1

    obj_labels = meta.get("labels") or {}
    pod_labels = _extract_pod_template_labels(job, "job")
    owner_name, owner_id, resolved_via = _resolve_owner(
        db, namespace=ns, name=name,
        obj_labels=obj_labels, pod_labels=pod_labels,
    )

    metadata_json: Dict[str, Any] = {
        "labels": obj_labels,
        "owner_label": owner_name,
        "owner_resolved_via": resolved_via,
        # Job чаще всего создаётся CronJob-ом, ownerReferences даёт parent.
        "owner_references": [
            {"kind": r.get("kind"), "name": r.get("name")}
            for r in (meta.get("ownerReferences") or [])
        ],
    }
    # name_pattern уже дал нам service_id — кладём сразу, не ждём
    # transitive линка через CronJob (Job может быть orphan: ad-hoc
    # `kubectl apply` без parent).
    if owner_id is not None:
        metadata_json["owner_service_id"] = owner_id
        stats["linked_via_name_pattern"] = (
            stats.get("linked_via_name_pattern", 0)
            + (1 if resolved_via == _RESOLVED_VIA_NAME_PATTERN else 0)
        )

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


def sync_all_jobs(db: Session) -> Dict[str, int]:
    """Sync все Job-ы cluster-wide.

    Returns stats:
        jobs_fetched
        nodes_upserted
        exit_codes_resolved      — сколько Failed Job-ов получили exit_code
        linked_via_name_pattern  — сколько Job-ов получили owner через
                                   name-pattern fallback (без labels)
        errors                   — Job-ы, откатившиеся per-item savepoint-ом
    """
    jobs = _kubectl_get_all("jobs")
    stats = {
        "jobs_fetched": len(jobs),
        "nodes_upserted": 0,
        "exit_codes_resolved": 0,
        "linked_via_name_pattern": 0,
        "errors": 0,
    }

    # Все внешние вызовы — здесь, до первого SQL: транзакция тика больше не
    # ждёт kubectl (см. _prefetch_failed_job_exit_codes).
    exit_codes = _prefetch_failed_job_exit_codes(jobs, stats)

    for i, job in enumerate(jobs, 1):
        try:
            # SAVEPOINT на item: одна битая запись (DataError и т.п.) не
            # переводит Session в aborted-состояние и не роняет весь tick.
            # Зеркалит per-item SAVEPOINT из k8s_events_sync.
            with db.begin_nested():
                _sync_one_job(db, job, stats, exit_codes=exit_codes)
        except Exception as e:
            stats["errors"] += 1
            logger.warning(
                "k8s_jobs_sync.job_failed ns=%s name=%s err=%s",
                (job.get("metadata") or {}).get("namespace"),
                (job.get("metadata") or {}).get("name"), e,
            )
        if i % _COMMIT_BATCH == 0:
            # Короткая транзакция = локи отпускаются, соседние писатели и DDL
            # не ждут конца всего тика.
            db.commit()

    db.commit()
    logger.info(
        "k8s_jobs_sync.jobs_done fetched=%d nodes=%d exit_codes=%d errors=%d",
        stats["jobs_fetched"], stats["nodes_upserted"],
        stats["exit_codes_resolved"], stats["errors"],
    )
    return stats


def _sync_one_cronjob(db: Session, cj: Dict[str, Any], stats: Dict[str, int]) -> None:
    """Обработать один CronJob. Вызывается под per-item SAVEPOINT-ом."""
    meta = cj.get("metadata") or {}
    ns = meta.get("namespace") or "default"
    name = meta.get("name")
    if not name:
        return

    cron_fields = _extract_cronjob_status(cj)
    obj_labels = meta.get("labels") or {}
    pod_labels = _extract_pod_template_labels(cj, "cronjob")
    owner_name, owner_id_from_resolver, resolved_via = _resolve_owner(
        db, namespace=ns, name=name,
        obj_labels=obj_labels, pod_labels=pod_labels,
    )

    metadata_json: Dict[str, Any] = {
        "labels": obj_labels,
        "owner_label": owner_name,
        "owner_resolved_via": resolved_via,
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
    # Используем отдельный edge-механизм: store в metadata_json.
    if not owner_name:
        stats["skipped_no_owner_label"] += 1
        return

    # Если resolver дал нам owner_id (name_pattern path), используем его
    # сразу. Иначе — ищем по label.
    if owner_id_from_resolver is not None:
        owner_svc_id: Optional[int] = owner_id_from_resolver
    else:
        owner_svc = (
            db.query(Service)
            .filter_by(namespace=ns, name=owner_name, node_kind=NODE_KIND_SERVICE)
            .one_or_none()
        )
        owner_svc_id = (
            cast(int, owner_svc.id) if owner_svc is not None else None
        )

    if owner_svc_id is None:
        stats["skipped_no_owner_match"] += 1
        return

    # Связь от owner Service → CronJob node храним в metadata_json
    # CronJob-а (owner_service_id). Это semantic «runs_as_job»: owner
    # Service _имеет_ CronJob как побочный workflow.
    meta_with_link: Dict[str, Any] = dict(cj_node.metadata_json or {})
    meta_with_link["owner_service_id"] = owner_svc_id
    cj_node.metadata_json = cast(Any, meta_with_link)
    db.flush()
    stats["edges_runs_as_job"] += 1
    if resolved_via == _RESOLVED_VIA_NAME_PATTERN:
        stats["linked_via_name_pattern"] += 1


def sync_all_cronjobs(db: Session) -> Dict[str, int]:
    """Sync все CronJob-ы cluster-wide + edge runs_as_job на owner Service.

    Returns stats:
        cronjobs_fetched
        nodes_upserted
        edges_runs_as_job        — сколько runs_as_job edges создано/обновлено
        skipped_no_owner_label   — owner не резолвится (ни label, ни name pattern)
        skipped_no_owner_match   — label есть, но в kg_services нет matching service
        linked_via_name_pattern  — из них сматчено name-pattern fallback'ом
        errors                   — CronJob-ы, откатившиеся per-item savepoint-ом
    """
    cronjobs = _kubectl_get_all("cronjobs")
    stats = {
        "cronjobs_fetched": len(cronjobs),
        "nodes_upserted": 0,
        "edges_runs_as_job": 0,
        "skipped_no_owner_label": 0,
        "skipped_no_owner_match": 0,
        "linked_via_name_pattern": 0,
        "errors": 0,
    }

    for cj in cronjobs:
        try:
            # SAVEPOINT на item — см. sync_all_jobs / k8s_events_sync.
            with db.begin_nested():
                _sync_one_cronjob(db, cj, stats)
        except Exception as e:
            stats["errors"] += 1
            logger.warning(
                "k8s_jobs_sync.cronjob_failed ns=%s name=%s err=%s",
                (cj.get("metadata") or {}).get("namespace"),
                (cj.get("metadata") or {}).get("name"), e,
            )

    db.commit()
    logger.info(
        "k8s_jobs_sync.cronjobs_done fetched=%d nodes=%d edges=%d "
        "skipped_no_label=%d skipped_no_match=%d errors=%d",
        stats["cronjobs_fetched"], stats["nodes_upserted"],
        stats["edges_runs_as_job"],
        stats["skipped_no_owner_label"], stats["skipped_no_owner_match"],
        stats["errors"],
    )
    return stats


def _link_jobs_to_cronjob_owners(db: Session) -> int:
    """После sync_all_jobs / sync_all_cronjobs: пройти Job-ы и проставить
    `owner_service_id` если parent CronJob уже имеет такую привязку.

    Это даёт transitive linkage: failed-Job (созданный CronJob-ом) сам
    знает к какому Service относится без повторного label-resolve.

    Возвращает кол-во linked Job-ов.

    Родители берутся ОДНИМ запросом. Раньше на каждый Job с
    ownerRef=CronJob шёл отдельный SELECT: на сотнях джоб — сотни запросов
    каждые 15 минут, все внутри одной транзакции.
    """
    linked = 0
    jobs = (
        db.query(K8sJob)
        .filter(K8sJob.kind == "job")
        .all()
    )

    # Первый проход — только чтение metadata (без SQL): собираем пары
    # (namespace, cronjob-name), по которым нужны родители.
    pending: List[Tuple[K8sJob, Dict[str, Any], str]] = []
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
        pending.append((j, meta, cj_name))

    if not pending:
        return 0

    namespaces = {str(j.namespace) for j, _, _ in pending}
    cj_names = {cj_name for _, _, cj_name in pending}
    parents = (
        db.query(K8sJob)
        .filter(
            K8sJob.kind == "cronjob",
            K8sJob.namespace.in_(namespaces),
            K8sJob.name.in_(cj_names),
        )
        .all()
    )
    # IN×IN может принести лишние пары (ns-A/cron-B, которого никто не просил) —
    # выбираем по точному ключу, лишнее просто не находится.
    owner_by_key: Dict[Tuple[str, str], Any] = {
        (str(p.namespace), str(p.name)): (
            cast(Dict[str, Any], p.metadata_json or {})
        ).get("owner_service_id")
        for p in parents
    }

    for j, meta, cj_name in pending:
        owner_id = owner_by_key.get((str(j.namespace), cj_name))
        if not owner_id:
            continue
        if meta.get("owner_service_id") == owner_id:
            continue
        meta_updated = dict(meta)
        meta_updated["owner_service_id"] = owner_id
        j.metadata_json = cast(Any, meta_updated)
        linked += 1
    if linked:
        db.commit()
    return linked


def cleanup_stale_jobs(
    db: Session,
    *,
    kind: str,
    fetch_count: int,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Bounded cleanup: удалить kg_k8s_jobs строки kind-а, не виденные
    sync-ом дольше N часов (Job/CronJob удалён из кластера).

    Fail-safe дисциплина (как drift_cleanup / edge-decay):
      * `fetch_count <= 0` → SKIP: `_kubectl_get_all` возвращает [] и при
        реальном сбое kubectl — пустой fetch неотличим от пустого кластера,
        чистить по нему нельзя (last_seen_at не обновился из-за сбоя);
      * удаление > _STALE_JOBS_MAX_DELETE_PCT% строк kind-а → SKIP
        (массовая «пропажа» — симптом сбоя, не реальной убыли);
      * удалённое логируется (sample имён).

    Порог возраста: settings.KG_K8S_JOBS_STALE_HOURS (default 48ч; sync
    ходит каждые 15 мин, так что 48ч — это ~192 пропущенных подтверждения).
    """
    from app.config import settings

    now = now or datetime.utcnow()
    max_age_hours = int(getattr(
        settings, "KG_K8S_JOBS_STALE_HOURS", _STALE_JOBS_MAX_AGE_HOURS_DEFAULT,
    ))
    stats: Dict[str, Any] = {"deleted": 0, "skipped": False, "reason": ""}

    if fetch_count <= 0:
        stats["skipped"] = True
        stats["reason"] = "empty_fetch"
        logger.warning(
            "k8s_jobs_sync.stale_cleanup_skipped kind=%s reason=empty_fetch — "
            "kubectl вернул 0 объектов (сбой неотличим от пустого кластера), "
            "чистка пропущена",
            kind,
        )
        return stats

    cutoff = now - timedelta(hours=max_age_hours)
    total = int(db.query(K8sJob).filter(K8sJob.kind == kind).count() or 0)
    if total == 0:
        return stats

    candidates = (
        db.query(K8sJob)
        .filter(
            K8sJob.kind == kind,
            # last_seen_at NULL у legacy-строк — судим по created_at.
            (
                (K8sJob.last_seen_at.isnot(None) & (K8sJob.last_seen_at < cutoff))
                | (K8sJob.last_seen_at.is_(None) & (K8sJob.created_at < cutoff))
            ),
        )
        .all()
    )
    if not candidates:
        return stats

    delete_pct = 100.0 * len(candidates) / total
    if delete_pct > _STALE_JOBS_MAX_DELETE_PCT:
        stats["skipped"] = True
        stats["reason"] = f"delete_pct={delete_pct:.1f}>max={_STALE_JOBS_MAX_DELETE_PCT:.1f}"
        logger.warning(
            "k8s_jobs_sync.stale_cleanup_skipped kind=%s reason=%s total=%d "
            "would_delete=%d — похоже на сбой источника, не чистим",
            kind, stats["reason"], total, len(candidates),
        )
        return stats

    sample = [f"{c.namespace}/{c.name}" for c in candidates[:20]]
    ids = [c.id for c in candidates]
    db.query(K8sJob).filter(K8sJob.id.in_(ids)).delete(synchronize_session=False)
    stats["deleted"] = len(candidates)
    logger.info(
        "k8s_jobs_sync.stale_cleanup kind=%s deleted=%d older_than_hours=%d "
        "sample=%s",
        kind, len(candidates), max_age_hours, sample,
    )
    return stats


def sync_k8s_jobs(db: Session) -> Dict[str, Any]:
    """Main entry: fetch + upsert обе resource-категории + чистка удалённых.

    CronJob-ы первыми чтобы Job-ы могли проследовать линк через
    ownerReferences→CronJob.owner_service_id (transitive linkage).
    """
    cj_stats = sync_all_cronjobs(db)
    job_stats = sync_all_jobs(db)
    transitive_linked = _link_jobs_to_cronjob_owners(db)
    # Cleanup после upsert-ов: живые строки только что получили свежий
    # last_seen_at, кандидаты в удаление — только реально исчезнувшие.
    stale_cleanup = {
        "cronjobs": cleanup_stale_jobs(
            db, kind="cronjob", fetch_count=cj_stats["cronjobs_fetched"],
        ),
        "jobs": cleanup_stale_jobs(
            db, kind="job", fetch_count=job_stats["jobs_fetched"],
        ),
    }
    db.commit()
    return {
        "cronjobs": cj_stats,
        "jobs": job_stats,
        "transitive_linked": transitive_linked,
        "stale_cleanup": stale_cleanup,
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
