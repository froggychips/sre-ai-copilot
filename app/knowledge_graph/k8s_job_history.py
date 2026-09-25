"""История состояний Job-ов (`kg_k8s_job_runs`) и запрос «на момент T».

Зачем отдельно от `kg_k8s_jobs`: там снимок, который перезаписывается
следующим запуском с тем же именем и удаляется вместе с исчезнувшим Job-ом.
Для разбора инцидента нужно другое — каким был migrate-job сквада, когда
поды начали крашиться, а не каким он стал через двое суток. См. докстринг
`schema.K8sJobRun`.

Запись дешёвая: `k8s_jobs_sync` один раз за тик читает последнее известное
состояние каждого Job-а (`latest_run_states`) и пишет строку только на
изменение (`record_job_run`). Обычный тик на стабильном кластере не пишет
ничего.

Чтение — `jobs_state_at(db, namespaces, at)`: последнее состояние каждого
Job-а, увиденное не позже `at`, с упавшими впереди. Это API для сборщика
KG-контекста инцидента.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from app.knowledge_graph.schema import K8sJobRun, Namespace

logger = logging.getLogger(__name__)

# Потолок длины message условия: kubelet пишет туда и многострочные
# описания, а истории нужна суть («Job has reached the specified backoff
# limit»), не лог.
_CONDITION_MESSAGE_MAX = 500

# Терминальные условия Job-а в порядке приоритета: Failed важнее Complete,
# если kubelet успел проставить оба (FailureTarget → Failed).
_TERMINAL_CONDITIONS = ("Failed", "FailureTarget", "Complete", "SuccessCriteriaMet")

_RETENTION_DAYS_DEFAULT = 30
_PRUNE_BATCH = 5000

# Поля, изменение которых = новая строка истории. owner_service_name сюда не
# входит: переатрибуция владельца не меняет того, что происходило с Job-ом.
_STATE_FIELDS = (
    "uid", "succeeded_count", "failed_count", "active_count",
    "completion_time", "last_pod_exit_code", "condition_type", "condition_reason",
    "disappeared",
)

StateKey = Tuple[Any, ...]


def terminal_condition(job: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """(type, reason, message) терминального условия со status=True или Nones."""
    conds = ((job.get("status") or {}).get("conditions")) or []
    active = {
        c.get("type"): c for c in conds
        if isinstance(c, dict) and str(c.get("status")).lower() == "true"
    }
    for t in _TERMINAL_CONDITIONS:
        c = active.get(t)
        if c:
            msg = c.get("message")
            if isinstance(msg, str) and len(msg) > _CONDITION_MESSAGE_MAX:
                msg = msg[:_CONDITION_MESSAGE_MAX] + "…"
            return t, c.get("reason"), msg
    return None, None, None


def state_key(fields: Dict[str, Any]) -> StateKey:
    # disappeared: None из свежего Job-а и False из БД — одно и то же
    # состояние, иначе каждый тик писал бы «изменение».
    return tuple(
        bool(fields.get(f)) if f == "disappeared" else fields.get(f)
        for f in _STATE_FIELDS
    )


def latest_run_states(db: Session) -> Dict[Tuple[str, str], StateKey]:
    """Последнее записанное состояние каждого Job-а: {(ns, name): state_key}.

    Один запрос на тик вместо запроса на Job: max(id) по (namespace, name)
    и join обратно. Индекс ix_kg_k8s_job_runs_ns_name покрывает группировку.
    """
    last = (
        db.query(K8sJobRun.namespace, K8sJobRun.name, func.max(K8sJobRun.id).label("mid"))
        .group_by(K8sJobRun.namespace, K8sJobRun.name)
        .subquery()
    )
    rows = db.query(K8sJobRun).join(last, K8sJobRun.id == last.c.mid).all()
    out: Dict[Tuple[str, str], StateKey] = {}
    for r in rows:
        out[(str(r.namespace), str(r.name))] = state_key(
            {f: getattr(r, f) for f in _STATE_FIELDS}
        )
    return out


def record_job_run(
    db: Session,
    *,
    namespace: str,
    name: str,
    fields: Dict[str, Any],
    prev: Dict[Tuple[str, str], StateKey],
    now: Optional[datetime] = None,
) -> bool:
    """Записать состояние, если оно отличается от последнего известного.

    `fields` — uid, счётчики, времена, exit-код, условие, owner. `prev`
    обновляется на месте: второй вызов в том же тике с тем же состоянием
    ничего не пишет. True — строка добавлена.
    """
    key = state_key(fields)
    if prev.get((namespace, name)) == key:
        return False
    db.add(K8sJobRun(
        namespace=namespace,
        name=name,
        uid=fields.get("uid"),
        owner_service_name=fields.get("owner_service_name"),
        succeeded_count=fields.get("succeeded_count"),
        failed_count=fields.get("failed_count"),
        active_count=fields.get("active_count"),
        start_time=fields.get("start_time"),
        completion_time=fields.get("completion_time"),
        last_pod_exit_code=fields.get("last_pod_exit_code"),
        condition_type=fields.get("condition_type"),
        condition_reason=fields.get("condition_reason"),
        condition_message=fields.get("condition_message"),
        disappeared=bool(fields.get("disappeared")),
        observed_at=now or datetime.utcnow(),
    ))
    prev[(namespace, name)] = key
    return True


def record_disappeared(
    db: Session,
    *,
    seen: Iterable[Tuple[str, str]],
    prev: Dict[Tuple[str, str], StateKey],
    now: Optional[datetime] = None,
) -> int:
    """Tombstone для Job-ов, которые были в истории, а в этом тике пропали.

    Вызывать только по полному листу (fetch вернул Job-ы): пустой ответ
    kubectl неотличим от пустого кластера, и по нему «удалить» всё нельзя —
    та же дисциплина, что у cleanup_stale_jobs. Tombstone несёт последнее
    известное состояние (счётчики, условие) и `disappeared=True`: упавший, а
    потом удалённый Job остаётся «упал», а незавершённый перестаёт быть
    «running» навсегда.
    """
    seen_set = set(seen)
    n = 0
    for (ns, name), key in list(prev.items()):
        if (ns, name) in seen_set:
            continue
        last = dict(zip(_STATE_FIELDS, key))
        if last.get("disappeared"):
            continue
        last["disappeared"] = True
        if record_job_run(db, namespace=ns, name=name, fields=last, prev=prev, now=now):
            n += 1
    return n


def prune_job_runs(
    db: Session,
    *,
    retention_days: Optional[int] = None,
    now: Optional[datetime] = None,
) -> int:
    """Удалить историю старше retention. Порция за вызов — без длинных локов.

    Последнюю строку каждого Job-а не трогаем, даже старую: без неё
    `latest_run_states` решит, что Job новый, и перепишет его состояние
    заново, а «состояние на момент T» для живого долгого CronJob-потомка
    потеряет опору.
    """
    from app.config import settings

    days = int(retention_days if retention_days is not None else getattr(
        settings, "KG_K8S_JOB_RUNS_RETENTION_DAYS", _RETENTION_DAYS_DEFAULT,
    ))
    cutoff = (now or datetime.utcnow()) - timedelta(days=days)
    keep = (
        db.query(func.max(K8sJobRun.id))
        .group_by(K8sJobRun.namespace, K8sJobRun.name)
        .subquery()
    )
    ids = [
        int(r[0]) for r in (
            db.query(K8sJobRun.id)
            .filter(K8sJobRun.observed_at < cutoff, ~K8sJobRun.id.in_(keep.select()))
            .limit(_PRUNE_BATCH)
            .all()
        )
    ]
    if not ids:
        return 0
    deleted = int(
        db.query(K8sJobRun).filter(K8sJobRun.id.in_(ids)).delete(synchronize_session=False)
        or 0
    )
    logger.info("k8s_job_history.pruned deleted=%d cutoff=%s", deleted, cutoff.isoformat())
    return deleted


def _get(r: Any, key: str) -> Any:
    """Поле строки истории: ORM-объект или словарь (строка `Select`)."""
    return r.get(key) if isinstance(r, dict) else getattr(r, key, None)


def _naive_ts(v: Any) -> Optional[datetime]:
    """Метка времени строки: naive UTC из сессии или ISO-строка из psql."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        try:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _row_dict(r: Any) -> Dict[str, Any]:
    return {
        "namespace": _get(r, "namespace"),
        "name": _get(r, "name"),
        "uid": _get(r, "uid"),
        "owner_service_name": _get(r, "owner_service_name"),
        "succeeded": _get(r, "succeeded_count"),
        "failed": _get(r, "failed_count"),
        "active": _get(r, "active_count"),
        "start_time": _naive_ts(_get(r, "start_time")),
        "completion_time": _naive_ts(_get(r, "completion_time")),
        "exit_code": _get(r, "last_pod_exit_code"),
        "condition_type": _get(r, "condition_type"),
        "condition_reason": _get(r, "condition_reason"),
        "condition_message": _get(r, "condition_message"),
        "observed_at": _naive_ts(_get(r, "observed_at")),
        "disappeared": bool(_get(r, "disappeared")),
        "status": _status(r),
    }


def _status(r: Any) -> str:
    cond = _get(r, "condition_type")
    active = _get(r, "active_count") or 0
    failed_n = _get(r, "failed_count") or 0
    failed = cond in ("Failed", "FailureTarget") or (
        cond is None and active == 0 and failed_n > 0
    )
    if _get(r, "disappeared"):
        # Удалён: «упал» остаётся фактом, всё остальное — уже не состояние.
        return "failed" if failed else "gone"
    if cond in ("Failed", "FailureTarget"):
        return "failed"
    if cond in ("Complete", "SuccessCriteriaMet"):
        return "succeeded"
    if active > 0:
        # Активный, но уже с упавшими попытками: backoff в процессе.
        return "retrying" if failed_n > 0 else "running"
    if failed_n > 0:
        return "failed"
    return "unknown"


_STATUS_ORDER = {"failed": 0, "retrying": 1, "running": 2, "unknown": 3, "succeeded": 4}


def incarnation_select(namespaces: List[str]) -> Select:
    """Начало текущей инкарнации namespace-ов (kg_namespaces.k8s_created_at)."""
    return (select(Namespace.namespace, Namespace.k8s_created_at)
            .where(Namespace.namespace.in_(namespaces)))


def incarnation_from_rows(rows: Iterable[Any]) -> Dict[str, datetime]:
    out: Dict[str, datetime] = {}
    for r in rows:
        created = _naive_ts(_get(r, "k8s_created_at"))
        if created is not None:
            out[str(_get(r, "namespace"))] = created
    return out


def _incarnation_starts(db: Session, namespaces: List[str]) -> Dict[str, datetime]:
    """Начало текущей инкарнации namespace-ов (kg_namespaces.k8s_created_at).

    Снесённый и пересозданный под тем же именем стенд — другой стенд: его
    прошлые Job-ы к инцидентам нового отношения не имеют. Нет строки или
    k8s_created_at — граница неизвестна, фильтра нет (так было и до истории).
    """
    try:
        return incarnation_from_rows(
            dict(m) for m in db.execute(incarnation_select(namespaces)).mappings())
    except Exception as e:  # граф без kg_namespaces — история всё равно полезна
        logger.debug("k8s_job_history.incarnation_lookup_failed err=%s", e)
        return {}


def last_runs_select(namespaces: List[str], at: datetime,
                     name_contains: Optional[str] = None) -> Select:
    """Последняя строка истории каждого (namespace, name) с observed_at <= at.

    `Select`, а не запрос сессии: тот же запрос исполняет и сборщик контекста
    инцидента через psql (датасет live-RCA), см. app/context/kg_incident_context.
    """
    at_n = _naive_ts(at) or at
    conds = [K8sJobRun.namespace.in_(namespaces), K8sJobRun.observed_at <= at_n]
    if name_contains:
        conds.append(K8sJobRun.name.ilike(f"%{name_contains}%"))
    last = (
        select(K8sJobRun.namespace, K8sJobRun.name, func.max(K8sJobRun.id).label("mid"))
        .where(*conds)
        .group_by(K8sJobRun.namespace, K8sJobRun.name)
        .subquery()
    )
    cols = [c for c in K8sJobRun.__table__.columns]
    return select(*cols).join(last, K8sJobRun.id == last.c.mid)


def states_at_from_rows(
    rows: Iterable[Any],
    incarnation: Dict[str, datetime],
    at: datetime,
    *,
    lookback_hours: int = 24,
    failed_lookback_hours: int = 168,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Отбор и порядок `jobs_state_at` поверх уже прочитанных строк."""
    at_n = _naive_ts(at) or at
    since = at_n - timedelta(hours=lookback_hours)
    failed_since = at_n - timedelta(hours=failed_lookback_hours)
    out = []
    for r in rows:
        d = _row_dict(r)
        born = incarnation.get(str(d["namespace"]))
        seen = d["observed_at"]
        if born is not None and born <= at_n and seen is not None and seen < born:
            # Строка из прошлой инкарнации namespace-а, а спрашивают про
            # нынешнюю: этот Job в новом стенде ещё не запускался.
            continue
        if d["status"] == "gone":
            continue
        recent = seen is not None and seen >= since
        unfinished = d["status"] in ("running", "retrying")
        failed_recently = d["status"] == "failed" and seen is not None and seen >= failed_since
        if recent or unfinished or failed_recently:
            out.append(d)
    out.sort(key=lambda d: (
        _STATUS_ORDER.get(d["status"], 9),
        -(d["observed_at"].timestamp() if d["observed_at"] else 0),
    ))
    return out[:limit]


def jobs_state_at(
    db: Session,
    namespaces: Iterable[str],
    at: datetime,
    *,
    lookback_hours: int = 24,
    failed_lookback_hours: int = 168,
    name_contains: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Состояние Job-ов namespace-ов на момент `at`.

    Для каждого (namespace, name) — последняя запись с `observed_at <= at`,
    и только если Job был виден в окне `[at - lookback, at]`, на момент `at`
    ещё не завершился, или упал не раньше `at - failed_lookback`. Последнее
    важно: строка пишется на изменение, и упавший позавчера migrate-job, так
    и висящий в Failed, в истории датирован позавчерашним днём — а к
    сегодняшнему крашу стенда он имеет прямое отношение. Успешный Job
    недельной давности — не имеет. Сортировка: failed → retrying → running → unknown →
    succeeded, затем по свежести. `status` — производное поле для
    потребителя, сырое условие — в condition_*.

    `name_contains` — фильтр подстрокой без учёта регистра, например
    "migrat" для migrate-job-ов.
    """
    ns_list = [n for n in namespaces if n]
    if not ns_list:
        return []
    incarnation = _incarnation_starts(db, ns_list)
    rows = [dict(m) for m in db.execute(last_runs_select(ns_list, at, name_contains)).mappings()]
    return states_at_from_rows(rows, incarnation, at, lookback_hours=lookback_hours,
                               failed_lookback_hours=failed_lookback_hours, limit=limit)
