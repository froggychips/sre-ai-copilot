"""Контекст инцидента из графа на момент времени — один сборщик на всех.

До этого модуля одно и то же знание графа собиралось тремя разными путями:

  * правила и модель пайплайна (`build_diagnostics_ctx` → `stage_diagnose`)
    получали из графа только соседние алерты по рёбрам зависимостей, а
    события подов и Job-ы — только из живого kubectl в момент разбора;
  * обогащение Discord-эмбеда (`enrich_alert`) читало события подов и деплои
    своими запросами — и только для эмбеда: модель их не видела;
  * датасет live-RCA (`scripts/live_rca_dataset.py`) держал третью, свою
    реконструкцию сырым SQL.

Модель и граф расходились: граф знал, что migrate-job сквада упал, а в промпт
это не попадало. Здесь — одна сборка. `fetch_kg_incident_context` читает граф
на момент `as_of` (данные после него не утекают: `last_seen` агрегата
kg_pod_events обрезается, `count` строки, обновлённой позже, неизвестен), а
`apply_kg_context` раскладывает результат в поля ctx, которые читают правила,
и `kg_context_prompt` — в текст для модели. Прод и датасет ходят в граф ОДНИМИ
И ТЕМИ ЖЕ запросами: объекты `Select` исполняются либо через SQLAlchemy-сессию
(`SessionReader`), либо — у датасета, у которого нет сессии к боевой БД, —
тем же SQL, отрендеренным в литералы и отправленным в psql (`PsqlReader`).

Источники (каждый со своим статусом: упал один — остальные на месте):

  kg_pod_events         события подов всего сквада (`squad-N-%`) в окне;
  kg_alerts             алерты сквада в окне, шумовые помечены;
  kg_deployments        деплой кода отдельно от пересборки статики и от
                        k8s_rollout (веерные раскатки не «недавний деплой»);
  kg_k8s_job_runs       Job-ы сквада на момент `as_of` из истории состояний
                        (`k8s_job_history`, упавшие впереди, инкарнация ns
                        учтена); без неё — снимок kg_k8s_jobs, где строка,
                        обновлённая позже `as_of`, помечается
                        `state_after_as_of`;
  kg_incidents          история: прошлые инциденты того же сквада;
  kg_remediation_events что внешний исполнитель (squad-medic) делал раньше и
                        чем кончилось, плюс его НАБЛЮДЕНИЯ как отдельный
                        источник `squad-medic` (без его выводов);
  kg_log_observations   ошибки из логов (Seq), если граф их знает.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import (Any, Callable, Dict, List, Optional, Protocol,
                    Sequence, Set, Tuple)

import structlog
from sqlalchemy import func, literal_column, or_, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Select

from app.context.collector import Collector, Outcome, SourceStatus
from app.context.medic_observations import PROVENANCE as MEDIC_PROVENANCE
from app.context.medic_observations import (observation_events,
                                            observations_of)
from app.knowledge_graph.schema import (AlertEvent, Deployment, K8sJob,
                                        KGIncident, KGRemediationEvent,
                                        LogObservation, PodEvent, Service)

log = structlog.get_logger()

SCHEMA = "kg_incident_ctx/v1"
#: Ключ ctx, под которым лежит собранный контекст графа целиком: его читают
#: промпт модели и снимок контекста; правила — только разложенные поля.
CTX_KEY = "kg_incident_context"

# Отсечка семантики `fixed` у squad-medic (external/mcp!115, раскатан
# 24.09.2026 08:56 UTC): после неё fixed=true — «стенд здоров», до — «что-то
# применил». В истории это разные утверждения, и модель должна их различать.
MEDIC_FIXED_SEMANTICS_CUTOVER = datetime(2026, 9, 24, 8, 56, tzinfo=timezone.utc)

_SQUAD_PREFIX_RE = re.compile(r"^(squad-[^-]+-)")
_NS_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,252}$")

# Причины событий подов, которые означают поломку, а не штатную жизнь пода.
BAD_POD_REASONS = frozenset({
    "BackOff", "CrashLoopBackOff", "Failed", "FailedCreate", "FailedMount",
    "FailedAttachVolume", "FailedScheduling", "ErrImagePull", "ImagePullBackOff",
    "ErrImageNeverPull", "InspectFailed", "CreateContainerConfigError",
    "CreateContainerError", "Unhealthy", "OOMKilling", "OOMKilled", "Evicted",
    "BackoffLimitExceeded", "DeadlineExceeded",
})
# GenerationMismatch в сквадах мигал от Rancher-churn (Rancher переписывал
# Deployment раз в минуту, controller отставал) — на сервисах с ingress, где
# ничего не сломано. Признак настоящего rollout-а — деплой этого сервиса в
# окне; без него алерт в выбор target-а не идёт.
NOISE_ALERTS = frozenset({"KubeDeploymentGenerationMismatch"})
# Провал пробы — симптом, а не поломка: его даёт любой rollout, пока новый
# под не прогрелся (без поправки target-ом становился town-grainhost от
# рестартов, которые медик сам делал каждые 2 часа).
_SOFT_REASONS = frozenset({"Unhealthy"})
_SOFT_WEIGHT = 0.2
# Метрика-источник, а не workload: алерты kube-state-metrics с неразобранной
# атрибуцией несут его имя в service.
NOT_TARGETS = frozenset({"vm-kube-state-metrics", "kube-state-metrics"})

_STATICS_MARK = "StaticsNewCluster"
_ROLLOUT_BUILDTYPE = "k8s_rollout"
_MIGRATE_TOKEN = "migrat"
_LOG_LEVELS = ("Error", "Fatal")

# Лимиты — это выборка, а не полный снимок: поля, наполненные отсюда,
# помечаются partial в source_status (см. apply_kg_context).
_POD_EVENT_SCAN = 400
_POD_EVENTS_KEEP = 40
_ALERTS_KEEP = 40
_DEPLOY_SCAN = 300
_DEPLOYS_KEEP = 10
_JOBS_KEEP = 30
_INCIDENTS_SCAN = 200
_REMEDIATION_KEEP = 30
_LOGS_KEEP = 10
_MSG_LEN = 300

PARTIAL_REASON = "partial: граф point-in-time, выборка окна, не полный снимок"


# --- чтение графа -----------------------------------------------------------


class KGReader(Protocol):
    """Исполнитель запросов к графу: строки — словари «колонка → значение»."""

    def rows(self, stmt: Select) -> List[Dict[str, Any]]: ...

    def has_column(self, table: str, column: str) -> bool: ...


class SessionReader:
    """Прод: те же `Select` через SQLAlchemy-сессию пайплайна/enrichment-а."""

    def __init__(self, db: Any) -> None:
        self.db = db
        self._columns: Dict[str, Set[str]] = {}

    def rows(self, stmt: Select) -> List[Dict[str, Any]]:
        return [dict(r._mapping) for r in self.db.execute(stmt)]

    def has_column(self, table: str, column: str) -> bool:
        if table not in self._columns:
            from sqlalchemy import inspect as sa_inspect
            try:
                cols = sa_inspect(self.db.get_bind()).get_columns(table)
                self._columns[table] = {c["name"] for c in cols}
            except Exception:
                self._columns[table] = set()
        return column in self._columns[table]


def render_sql(stmt: Select) -> str:
    """`Select` → SQL-литерал для PostgreSQL (тот же запрос, что у сессии).

    Значения — только имена namespace (проверены `_NS_RE`), числа и метки
    времени: literal_binds экранирует кавычки сам.
    """
    return str(stmt.compile(dialect=postgresql.dialect(),
                            compile_kwargs={"literal_binds": True}))


class PsqlReader:
    """Датасет: у скрипта нет сессии к боевой БД, только `kubectl exec psql`.

    Запросы — те же объекты `Select`, отрендеренные `render_sql`; каждая
    строка результата приходит как `to_jsonb`, READ ONLY-транзакция. Сам
    транспорт (`run_psql`) — у вызывающего: прямых вызовов kubectl в app/ нет.
    """

    def __init__(self, run_psql: Callable[[str], Optional[str]]) -> None:
        self.run_psql = run_psql
        self._columns: Dict[str, Set[str]] = {}

    def _json_rows(self, sql: str) -> List[Dict[str, Any]]:
        # `sql` — только результат render_sql(Select) (значения экранирует
        # literal_binds SQLAlchemy), не текст снаружи; обёртка — транзакция
        # READ ONLY и построчный JSON.
        wrapped = ("BEGIN; SET TRANSACTION READ ONLY; "
                   f"SELECT to_jsonb(t) FROM ({sql}) t; COMMIT;")  # nosec B608
        out = self.run_psql(wrapped)
        if out is None:
            raise RuntimeError("psql не ответил")
        return [json.loads(ln) for ln in out.splitlines() if ln.startswith("{")]

    def rows(self, stmt: Select) -> List[Dict[str, Any]]:
        return self._json_rows(render_sql(stmt))

    def has_column(self, table: str, column: str) -> bool:
        if table not in self._columns:
            if not re.fullmatch(r"[a-z_]+", table):
                return False
            stmt: Select = (select(literal_column("column_name"))
                    .select_from(text("information_schema.columns"))
                    .where(literal_column("table_name") == table))
            rows = self.rows(stmt)
            self._columns[table] = {str(r["column_name"]) for r in rows if r.get("column_name")}
        return column in self._columns[table]


# --- время ------------------------------------------------------------------


def _aware(v: Any) -> Optional[datetime]:
    """Метка времени из сессии (naive UTC), psql (ISO-строка) или ISO с зоной."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        try:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _naive(dt: datetime) -> datetime:
    """Граф хранит naive UTC: сравнение в запросе — в той же форме."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _jsonish(v: Any) -> Any:
    """JSON-колонка: из сессии — объект, из psql — объект, из sqlite — строка."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


# --- скоуп ------------------------------------------------------------------


@dataclass(frozen=True)
class KGScope:
    """Что и на какой момент собирать.

    `exclude_remediation_ids` — события исполнителя, которые не должны попасть
    в ИСТОРИЮ (у датасета — сам разбор, чей вывод и есть ответ кейса); их
    наблюдения при этом остаются: медик снял их с живого стенда в момент
    `as_of`. `with_conclusions=False` убирает из истории выводы исполнителя
    (root_cause) — в проде это законное знание «так уже было», в оценке —
    подсказка.
    """

    namespace: str
    as_of: datetime
    service: Optional[str] = None
    alertname: Optional[str] = None
    event_window_min: int = 120
    deploy_window_min: int = 360
    history_days: int = 14
    with_conclusions: bool = True
    exclude_remediation_ids: Tuple[int, ...] = ()

    @property
    def as_of_utc(self) -> datetime:
        a = _aware(self.as_of)
        if a is None:
            raise ValueError("KGScope.as_of обязателен")
        return a


def squad_prefix(namespace: str) -> Optional[str]:
    """`squad-19-shared` → `squad-19-`: стенд живёт в нескольких namespace."""
    m = _SQUAD_PREFIX_RE.match(namespace or "")
    return m.group(1) if m else None


def _scope_namespaces(reader: KGReader, namespace: str) -> List[str]:
    """Все namespace стенда из kg_services (таблица маленькая) + сам namespace.

    Точный джойн по namespace находил деплои у 7% кейсов, по скваду — у 76%:
    медик и алерты пишут `squad-N-shared`, а деплои и события сервисов
    ложатся в `squad-N-kingdomX`.
    """
    prefix = squad_prefix(namespace)
    found: List[str] = []
    if prefix:
        stmt = (select(Service.namespace).where(Service.namespace.like(prefix + "%"))
                .distinct())
        found = sorted({r["namespace"] for r in reader.rows(stmt) if r.get("namespace")})
    out = [n for n in found if _NS_RE.match(n)]
    if namespace not in out:
        out.insert(0, namespace)
    return out


# --- источники ----------------------------------------------------------------


def _pod_events(reader: KGReader, ns: List[str], scope: KGScope) -> List[Dict[str, Any]]:
    from app.services.pii_redaction import redact_pii

    as_of = scope.as_of_utc
    lo = as_of - timedelta(minutes=scope.event_window_min)
    last_act = func.coalesce(PodEvent.last_seen, PodEvent.first_seen)
    stmt = (
        select(PodEvent.namespace, PodEvent.pod_name, PodEvent.type, PodEvent.reason,
               PodEvent.message, PodEvent.count, PodEvent.first_seen, PodEvent.last_seen,
               Service.name.label("service"))
        .outerjoin(Service, Service.id == PodEvent.service_id)
        .where(
            PodEvent.namespace.in_(ns),
            # Нижняя граница first_seen — ради индекса: без неё запрос сканирует
            # историю с апреля. Строка старше 7 суток, всё ещё обновлявшаяся,
            # теряется — цена приемлемая.
            PodEvent.first_seen >= _naive(as_of - timedelta(days=7)),
            PodEvent.first_seen <= _naive(as_of),
            last_act >= _naive(lo),
        )
        .order_by(last_act.desc())
        .limit(_POD_EVENT_SCAN)
    )
    latest: Dict[Tuple[Any, Any, Any], Dict[str, Any]] = {}
    for r in reader.rows(stmt):
        first = _aware(r.get("first_seen"))
        last = _aware(r.get("last_seen")) or first
        if first is None or last is None:
            continue
        # kg_pod_events — изменяемый агрегат: count и last_seen дописываются
        # следующими синками. last_seen обрезаем по as_of, а count строки,
        # обновлённой ПОСЛЕ него, на момент инцидента неизвестен.
        eff_last = min(last, as_of)
        ev = {
            "namespace": r.get("namespace"),
            "pod": r.get("pod_name"),
            "service": r.get("service"),
            "type": r.get("type"),
            "reason": r.get("reason"),
            "message": redact_pii(str(r.get("message") or ""), max_len=_MSG_LEN),
            "count": r.get("count") if last <= as_of else None,
            "first_seen": _iso(first),
            "last_seen": _iso(eff_last),
        }
        # Последнее событие на (под, reason): иначе лимит съедают сотни
        # одинаковых Unhealthy от readiness-пробы, а BackOff не влезает.
        key = (ev["namespace"], ev["pod"], ev["reason"])
        prev = latest.get(key)
        if prev is None or (ev["last_seen"] or "") > (prev["last_seen"] or ""):
            latest[key] = ev
    events = sorted(
        latest.values(),
        key=lambda e: (e["namespace"] != scope.namespace, e.get("type") != "Warning",
                       _neg_ts(e.get("last_seen"))),
    )
    return events[:_POD_EVENTS_KEEP]


def _neg_ts(v: Optional[str]) -> float:
    t = _aware(v)
    return -t.timestamp() if t else 0.0


def _alerts(reader: KGReader, ns: List[str], scope: KGScope) -> List[Dict[str, Any]]:
    as_of = scope.as_of_utc
    lo = as_of - timedelta(minutes=scope.event_window_min)
    stmt = (
        select(AlertEvent.alertname, AlertEvent.severity, AlertEvent.fired_at,
               AlertEvent.resolved_at, AlertEvent.fingerprint,
               Service.namespace, Service.name.label("service"),
               KGIncident.extras.label("incident_extras"))
        .join(Service, Service.id == AlertEvent.service_id)
        .outerjoin(KGIncident, KGIncident.incident_key == AlertEvent.incident_id)
        .where(Service.namespace.in_(ns),
               AlertEvent.fired_at >= _naive(lo),
               AlertEvent.fired_at <= _naive(as_of))
        .order_by(AlertEvent.fired_at.desc())
        .limit(_ALERTS_KEEP)
    )
    out: List[Dict[str, Any]] = []
    for r in reader.rows(stmt):
        resolved = _aware(r.get("resolved_at"))
        extras = _jsonish(r.get("incident_extras")) or {}
        marked = (extras.get("noise_fingerprints") or {}) if isinstance(extras, dict) else {}
        out.append({
            "namespace": r.get("namespace"),
            "service": r.get("service"),
            "alertname": r.get("alertname"),
            "severity": r.get("severity"),
            "fired_at": _iso(_aware(r.get("fired_at"))),
            # Резолв после as_of — будущее для инцидента.
            "resolved_at": _iso(resolved) if resolved and resolved <= as_of else None,
            "noise_kinds": sorted(marked.get(r.get("fingerprint") or "") or []),
        })
    return out


def _deploy_kind(buildtype_id: Optional[str]) -> str:
    b = buildtype_id or ""
    if _STATICS_MARK in b:
        return "statics"
    if b == _ROLLOUT_BUILDTYPE:
        return "rollout"
    return "code"


def _deployments(reader: KGReader, ns: List[str], scope: KGScope) -> Dict[str, Any]:
    from app.knowledge_graph.queries import deploy_attribution_scope

    as_of = scope.as_of_utc
    lo = as_of - timedelta(minutes=scope.deploy_window_min)
    stmt = (
        select(Deployment.started_at, Deployment.finished_at, Deployment.status,
               Deployment.buildtype_id, Deployment.build_number, Deployment.sha,
               Deployment.extras, Service.namespace, Service.name.label("service"))
        .join(Service, Service.id == Deployment.service_id)
        .where(Service.namespace.in_(ns),
               Deployment.started_at >= _naive(lo),
               Deployment.started_at <= _naive(as_of))
        .order_by(Deployment.started_at.desc())
        .limit(_DEPLOY_SCAN)
    )
    code: List[Dict[str, Any]] = []
    rollouts: List[Dict[str, Any]] = []
    statics = 0
    seen: Set[Tuple[Any, ...]] = set()
    for r in reader.rows(stmt):
        kind = _deploy_kind(r.get("buildtype_id"))
        if kind == "statics":
            # Статика — только счётчиком: веерная раскатка на все сервисы
            # сквада сотнями строк заняла бы весь лимит и сделала «недавний
            # деплой» истиной почти для любого кейса.
            statics += 1
            continue
        finished = _aware(r.get("finished_at"))
        extras = _jsonish(r.get("extras"))
        d = {
            "namespace": r.get("namespace"),
            "service": r.get("service"),
            "kind": kind,
            "status": r.get("status"),
            "buildtype_id": r.get("buildtype_id"),
            "number": r.get("build_number"),
            "sha": r.get("sha"),
            "started_at": _iso(_aware(r.get("started_at"))),
            "finished_at": _iso(finished) if finished and finished <= as_of else None,
            "attribution_scope": deploy_attribution_scope(extras if isinstance(extras, dict) else {}),
        }
        key = (d["namespace"], d["service"], d["buildtype_id"], d["number"], d["started_at"])
        if key in seen:
            continue
        seen.add(key)
        bucket = code if kind == "code" else rollouts
        if len(bucket) < _DEPLOYS_KEEP:
            bucket.append(d)
    return {"code": code, "rollouts": rollouts, "statics_count": statics}


def _jobs(reader: KGReader, ns: List[str], scope: KGScope) -> List[Dict[str, Any]]:
    """Job-ы сквада на момент as_of: из истории, если она есть, иначе из снимка."""
    if reader.has_column("kg_k8s_job_runs", "observed_at"):
        return _job_history(reader, ns, scope)
    return _job_snapshot(reader, ns, scope)


def _job_history(reader: KGReader, ns: List[str], scope: KGScope) -> List[Dict[str, Any]]:
    """История kg_k8s_job_runs (#454): последнее состояние Job-а, увиденное не
    позже as_of, упавшие впереди — те же запрос и отбор, что у
    `k8s_job_history.jobs_state_at`, исполненные через reader (прод/psql)."""
    from app.knowledge_graph import k8s_job_history as jh

    as_of = scope.as_of_utc
    try:
        incarnation = jh.incarnation_from_rows(reader.rows(jh.incarnation_select(ns)))
    except Exception:  # граф без kg_namespaces — история всё равно полезна
        incarnation = {}
    states = jh.states_at_from_rows(reader.rows(jh.last_runs_select(ns, as_of)),
                                    incarnation, as_of, limit=_JOBS_KEEP)
    return [{
        "namespace": d["namespace"],
        "name": d["name"],
        "owner_service": d["owner_service_name"],
        "succeeded": d["succeeded"],
        "failed": d["failed"],
        "active": d["active"],
        "exit_code": d["exit_code"],
        "status": d["status"],
        "condition_reason": d["condition_reason"],
        "start_time": _iso(_aware(d["start_time"])),
        "completion_time": _iso(_aware(d["completion_time"])),
        "observed_at": _iso(_aware(d["observed_at"])),
        # История пишется на изменение: строка на as_of — наблюдение на as_of.
        "state_after_as_of": False,
        "migrate": _MIGRATE_TOKEN in str(d["name"] or "").lower(),
        "source": "kg_k8s_job_runs",
    } for d in states]


def _job_snapshot(reader: KGReader, ns: List[str], scope: KGScope) -> List[Dict[str, Any]]:
    as_of = scope.as_of_utc
    started = func.coalesce(K8sJob.start_time, K8sJob.created_at)
    stmt = (
        select(K8sJob.namespace, K8sJob.name, K8sJob.kind, K8sJob.owner_service_name,
               K8sJob.succeeded_count, K8sJob.failed_count, K8sJob.active_count,
               K8sJob.start_time, K8sJob.completion_time, K8sJob.last_pod_exit_code,
               K8sJob.last_seen_at)
        .where(K8sJob.namespace.in_(ns), K8sJob.kind == "Job",
               started <= _naive(as_of),
               # Job, которого синк не видел сутки до as_of, к инциденту не
               # относится (снесён или устарел).
               or_(K8sJob.last_seen_at.is_(None),
                   K8sJob.last_seen_at >= _naive(as_of - timedelta(hours=24))))
        .order_by(K8sJob.failed_count.desc(), started.desc())
        .limit(_JOBS_KEEP)
    )
    out: List[Dict[str, Any]] = []
    for r in reader.rows(stmt):
        completed = _aware(r.get("completion_time"))
        seen = _aware(r.get("last_seen_at"))
        # kg_k8s_jobs — снимок, а не история: строку перезаписывает каждый
        # синк. Если её видели заметно позже as_of, счётчики могли
        # измениться уже после инцидента — это не наблюдение на as_of.
        after = bool((completed and completed > as_of)
                     or (seen and seen > as_of + timedelta(minutes=30)))
        out.append({
            "namespace": r.get("namespace"),
            "name": r.get("name"),
            "owner_service": r.get("owner_service_name"),
            "succeeded": r.get("succeeded_count"),
            "failed": r.get("failed_count"),
            "active": r.get("active_count"),
            "exit_code": r.get("last_pod_exit_code"),
            "start_time": _iso(_aware(r.get("start_time"))),
            "completion_time": _iso(completed) if completed and completed <= as_of else None,
            "state_after_as_of": after,
            "migrate": _MIGRATE_TOKEN in str(r.get("name") or "").lower(),
            "source": "kg_k8s_jobs",
        })
    return out


def _incident_history(reader: KGReader, ns: List[str], scope: KGScope) -> List[Dict[str, Any]]:
    as_of = scope.as_of_utc
    stmt = (
        select(KGIncident.namespace, KGIncident.service_name, KGIncident.alertnames,
               KGIncident.opened_at, KGIncident.resolved_at, KGIncident.resolve_reason,
               KGIncident.noise)
        .where(KGIncident.namespace.in_(ns),
               KGIncident.opened_at >= _naive(as_of - timedelta(days=scope.history_days)),
               KGIncident.opened_at < _naive(as_of))
        .order_by(KGIncident.opened_at.desc())
        .limit(_INCIDENTS_SCAN)
    )
    agg: Dict[Tuple[Any, Any], Dict[str, Any]] = {}
    for r in reader.rows(stmt):
        names = _jsonish(r.get("alertnames")) or []
        first = names[0] if isinstance(names, list) and names else None
        key = (r.get("service_name"), first)
        a = agg.setdefault(key, {"service": key[0], "alertname": first, "count": 0,
                                 "noise": 0, "last_opened_at": None,
                                 "last_resolve_reason": None})
        a["count"] += 1
        if r.get("noise"):
            a["noise"] += 1
        opened = _iso(_aware(r.get("opened_at")))
        if a["last_opened_at"] is None or (opened or "") > a["last_opened_at"]:
            a["last_opened_at"] = opened
            resolved = _aware(r.get("resolved_at"))
            a["last_resolve_reason"] = (r.get("resolve_reason")
                                        if resolved and resolved <= as_of else None)
    return sorted(agg.values(), key=lambda a: -a["count"])[:10]


def _remediation(reader: KGReader, ns: List[str], scope: KGScope
                 ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(история прогонов исполнителя, наблюдения squad-medic в окне)."""
    as_of = scope.as_of_utc
    cols: List[Any] = [KGRemediationEvent.id, KGRemediationEvent.actor, KGRemediationEvent.namespace,
            KGRemediationEvent.namespaces, KGRemediationEvent.started_at,
            KGRemediationEvent.finished_at, KGRemediationEvent.outcome,
            KGRemediationEvent.fixed, KGRemediationEvent.still_unhealthy,
            KGRemediationEvent.applied, KGRemediationEvent.manual, KGRemediationEvent.gaps,
            KGRemediationEvent.summary, KGRemediationEvent.extras,
            KGRemediationEvent.root_cause]
    if reader.has_column("kg_remediation_events", "observations"):
        cols.append(KGRemediationEvent.observations)
    stmt = (
        select(*cols)
        .where(KGRemediationEvent.namespace.in_(ns),
               KGRemediationEvent.started_at >= _naive(as_of - timedelta(days=scope.history_days)),
               KGRemediationEvent.started_at <= _naive(as_of))
        .order_by(KGRemediationEvent.started_at.desc())
        .limit(_REMEDIATION_KEEP)
    )
    history: List[Dict[str, Any]] = []
    observed: List[Dict[str, Any]] = []
    obs_lo = as_of - timedelta(minutes=scope.event_window_min)
    for r in reader.rows(stmt):
        for k in ("namespaces", "applied", "manual", "gaps", "extras", "observations"):
            if k in r:
                r[k] = _jsonish(r[k])
        started = _aware(r.get("started_at"))
        finished = _aware(r.get("finished_at"))
        if started and started >= obs_lo:
            # Наблюдения сняты с живого стенда в момент начала прогона —
            # наблюдаемое на as_of, даже если сам прогон ещё идёт.
            obs = observations_of(r)
            if obs.get("facts"):
                observed.append({"event_id": r.get("id"), "observed_at": obs.get("observed_at")
                                 or _iso(started), "namespace": r.get("namespace"),
                                 "facts": list(obs["facts"])})
        if r.get("id") in scope.exclude_remediation_ids:
            continue
        if not finished or finished > as_of:
            continue  # итог прогона — будущее для инцидента
        entry = {
            "event_id": r.get("id"),
            "actor": r.get("actor"),
            "namespace": r.get("namespace"),
            "started_at": _iso(started),
            "outcome": r.get("outcome"),
            "fixed": bool(r.get("fixed")),
            "fixed_semantics": ("healthy" if started and started >= MEDIC_FIXED_SEMANTICS_CUTOVER
                                else "applied_something"),
            "applied": [str(a)[:120] for a in (r.get("applied") or [])][:5],
        }
        if scope.with_conclusions and r.get("root_cause"):
            entry["root_cause"] = str(r["root_cause"])[:200]
        history.append(entry)
    return history, observed


def _logs(reader: KGReader, ns: List[str], scope: KGScope) -> List[Dict[str, Any]]:
    from app.services.pii_redaction import redact_pii

    as_of = scope.as_of_utc
    lo = as_of - timedelta(minutes=scope.event_window_min)
    stmt = (
        select(LogObservation.namespace, LogObservation.app_name, LogObservation.level,
               LogObservation.count, LogObservation.sample_message, LogObservation.ts)
        .where(LogObservation.namespace.in_(ns),
               LogObservation.level.in_(_LOG_LEVELS),
               LogObservation.ts >= _naive(lo),
               LogObservation.ts <= _naive(as_of))
        .order_by(LogObservation.count.desc())
        .limit(_LOGS_KEEP)
    )
    return [{"namespace": r.get("namespace"), "app": r.get("app_name"),
             "level": r.get("level"), "count": r.get("count"),
             "message": redact_pii(str(r.get("sample_message") or ""), max_len=_MSG_LEN),
             "ts": _iso(_aware(r.get("ts")))} for r in reader.rows(stmt)]


# --- target: сломанный workload, а не ближайший инцидент --------------------

# Алфавит суффиксов k8s (без гласных и 0/1/3) — чтобы `town-db-0` не резался
# как job-под, а `map-service` не терял «service».
_K8S_SFX = "[bcdfghjklmnpqrstvwxz2456789]"
_RS_POD_RE = re.compile(rf"^(?P<w>.+)-{_K8S_SFX}{{6,10}}-{_K8S_SFX}{{5}}$")
_CRONJOB_POD_RE = re.compile(rf"^(?P<w>.+)-\d{{8,10}}-{_K8S_SFX}{{5}}$")
_STS_POD_RE = re.compile(r"^(?P<w>.+)-\d{1,3}$")
_JOB_POD_RE = re.compile(rf"^(?P<w>.+)-{_K8S_SFX}{{5}}$")


def workload_of(pod: str) -> str:
    """Имя workload-а по имени пода: Deployment (`x-<rs>-<pod>`), CronJob
    (`x-<ts>-<pod>`), StatefulSet (`x-0`), Job (`x-<pod>`)."""
    for rx in (_RS_POD_RE, _CRONJOB_POD_RE, _STS_POD_RE, _JOB_POD_RE):
        m = rx.match(pod or "")
        if m:
            return m.group("w")
    return pod or ""


def _proximity(ts: Any, as_of: Optional[datetime]) -> float:
    """Вес по близости к as_of: событие за 5 минут важнее двухчасового
    (1.0 → ~0.2 к краю окна)."""
    t = _aware(ts)
    if not t or not as_of:
        return 0.5
    age_min = max((as_of - t).total_seconds() / 60.0, 0.0)
    return 1.0 / (1.0 + age_min / 30.0)


def select_targets(kgc: Dict[str, Any], medic_action_text: str = "",
                   limit: int = 5) -> List[Dict[str, Any]]:
    """Кандидаты в target, самый «больной» первым.

    Вес workload-а — сумма по его плохим событиям подов (count, обрезанный до
    20, × близость к as_of) плюс не-шумовые алерты его сервиса. Провал пробы
    у workload-а, который в окне раскатывался, не считается (прогрев нового
    пода), в остальных случаях — с весом 0.2. Имена, которые исполнитель
    раньше назвал в своих действиях, только УСИЛИВАЮТ известных кандидатов с
    «жёсткой» причиной (×1.5): нового имени из текста не рождается.
    """
    as_of = _aware(kgc.get("as_of"))
    deploys = kgc.get("deployments") or {}
    rolled = {(d.get("namespace"), d.get("service"))
              for d in list(deploys.get("code") or []) + list(deploys.get("rollouts") or [])}
    acc: Dict[Tuple[Any, str], Dict[str, Any]] = {}

    def slot(ns: Optional[str], wl: str) -> Dict[str, Any]:
        return acc.setdefault((ns, wl), {"namespace": ns, "workload": wl, "score": 0.0,
                                         "reasons": set(), "sources": set()})

    for e in kgc.get("pod_events") or []:
        reason = e.get("reason")
        if reason not in BAD_POD_REASONS:
            continue
        wl = workload_of(e.get("pod") or "")
        if not wl or wl in NOT_TARGETS:
            continue
        weight = 1.0
        if reason in _SOFT_REASONS:
            if (e.get("namespace"), wl) in rolled:
                continue
            weight = _SOFT_WEIGHT
        t = slot(e.get("namespace"), wl)
        t["score"] += weight * min(int(e.get("count") or 1), 20) * _proximity(e.get("last_seen"), as_of)
        t["reasons"].add(reason)
        t["sources"].add("kg_pod_events")
    for a in kgc.get("alerts") or []:
        svc, ns = a.get("service"), a.get("namespace")
        if not svc or svc in NOT_TARGETS or a.get("resolved_at") or a.get("noise_kinds"):
            continue
        if a.get("alertname") in NOISE_ALERTS and (ns, svc) not in rolled:
            continue
        t = slot(ns, svc)
        t["score"] += 5.0 * _proximity(a.get("fired_at"), as_of)
        t["reasons"].add(a.get("alertname") or "alert")
        t["sources"].add("kg_alerts")
    if medic_action_text:
        for t in acc.values():
            if not (t["reasons"] - _SOFT_REASONS):
                continue
            if re.search(rf"(?<![\w-]){re.escape(t['workload'])}(?![\w-])", medic_action_text):
                t["score"] *= 1.5
                t["sources"].add("medic_applied")
    ranked = sorted(acc.values(), key=lambda t: -t["score"])[:limit]
    return [{**t, "score": round(t["score"], 2), "reasons": sorted(t["reasons"]),
             "sources": sorted(t["sources"])} for t in ranked if t["score"] > 0]


# --- сборка -------------------------------------------------------------------

_SOURCES: Sequence[Tuple[str, Callable[..., Any]]] = (
    ("kg_pod_events", _pod_events),
    ("kg_alerts", _alerts),
    ("kg_deployments", _deployments),
    ("kg_k8s_jobs", _jobs),
    ("kg_incidents", _incident_history),
    ("kg_remediation_events", _remediation),
    ("kg_log_observations", _logs),
)


def fetch_kg_incident_context(reader: KGReader, scope: KGScope) -> Dict[str, Any]:
    """Прочитать граф на момент `scope.as_of`. Упавший источник — запись в
    `sources` с причиной, остальные на месте; JSON-сериализуемо (метки времени
    — ISO), чтобы датасет хранил ровно то, что увидел бы прод."""
    namespaces = _scope_namespaces(reader, scope.namespace)
    kgc: Dict[str, Any] = {
        "schema": SCHEMA,
        "as_of": _iso(scope.as_of_utc),
        "namespace": scope.namespace,
        "service": scope.service,
        "alertname": scope.alertname,
        "ns_scope": (squad_prefix(scope.namespace) or scope.namespace) + (
            "%" if squad_prefix(scope.namespace) else ""),
        "namespaces": namespaces,
        "pod_events": [], "alerts": [],
        "deployments": {"code": [], "rollouts": [], "statics_count": 0},
        "jobs": [], "incident_history": [], "remediation_history": [],
        "medic_observations": [], "logs": [],
        "sources": {},
    }
    for name, fn in _SOURCES:
        try:
            data = fn(reader, namespaces, scope)
        except Exception as e:  # источник, а не сборка: остальные должны прийти
            log.warning("kg_context.source_failed", source=name, error=type(e).__name__,
                        message=str(e)[:200])
            kgc["sources"][name] = {"status": SourceStatus.FAILED.value,
                                    "reason": f"{name} недоступен: {type(e).__name__}"}
            continue
        if name == "kg_pod_events":
            kgc["pod_events"] = data
        elif name == "kg_alerts":
            kgc["alerts"] = data
        elif name == "kg_deployments":
            kgc["deployments"] = data
        elif name == "kg_k8s_jobs":
            kgc["jobs"] = data
        elif name == "kg_incidents":
            kgc["incident_history"] = data
        elif name == "kg_remediation_events":
            kgc["remediation_history"], kgc["medic_observations"] = data
        elif name == "kg_log_observations":
            kgc["logs"] = data
        empty = not data or (isinstance(data, tuple) and not any(data)) or (
            isinstance(data, dict) and not (data.get("code") or data.get("rollouts")
                                            or data.get("statics_count")))
        kgc["sources"][name] = {"status": (SourceStatus.EMPTY if empty
                                           else SourceStatus.SUCCESS).value}
    kgc["targets"] = select_targets(kgc, _medic_action_text(kgc))
    return kgc


def _medic_action_text(kgc: Dict[str, Any]) -> str:
    return json.dumps([h.get("applied") or [] for h in kgc.get("remediation_history") or []],
                      ensure_ascii=False)


KG_CONTEXT = Collector(
    name="kg_incident_context",
    ctx_fields=("k8s_events", "recent_deployments", "kg_jobs"),
    provenance="kg_pod_events+kg_alerts+kg_deployments+kg_k8s_jobs+kg_incidents+"
               "kg_remediation_events+kg_log_observations",
    failure_label="граф недоступен",
    log_event="kg_context.failed",
)


def collect_kg_incident_context(reader: KGReader, scope: KGScope) -> Any:
    """Для `KG_CONTEXT.run_sync`: частичный отказ источников — PARTIAL."""
    kgc = fetch_kg_incident_context(reader, scope)
    failed = [n for n, s in kgc["sources"].items() if s["status"] == SourceStatus.FAILED.value]
    if failed and len(failed) == len(_SOURCES):
        return Outcome(SourceStatus.FAILED, kgc, reason="граф недоступен: все источники упали")
    return Outcome(SourceStatus.PARTIAL if failed else SourceStatus.SUCCESS, kgc)


# --- раскладка в ctx правил --------------------------------------------------

# Какое поле ctx наполняет какой источник: упал источник — поле в
# source_status, и правило ответит «?», а не уверенное «не было».
_SOURCE_FIELDS = {
    "kg_pod_events": ("k8s_events",),
    "kg_deployments": ("recent_deployments",),
    "kg_k8s_jobs": ("kg_jobs",),
}


def _block(ctx: Dict[str, Any], field: str, source: str, lines: List[str]) -> None:
    """Наблюдаемый текст с маркером источника. Не в analyzer_summary: тот —
    проза модели и в text_haystack правил не входит намеренно."""
    if not lines:
        return
    block = f"[{source}]\n" + "\n".join(lines)
    prev = ctx.get(field)
    ctx[field] = f"{prev}\n{block}" if prev else block


def _event_line(e: Dict[str, Any]) -> str:
    reason, msg = e.get("reason") or "", (e.get("message") or "").strip()
    cnt = f" x{e['count']}" if e.get("count") else ""
    return f"{e.get('type') or 'Event'} {reason}{cnt}: {msg}".rstrip(": ")


def _job_line(j: Dict[str, Any]) -> str:
    state = f"failed={j.get('failed') or 0} succeeded={j.get('succeeded') or 0}"
    if j.get("status"):
        state = f"status={j['status']} " + state
    if j.get("exit_code") is not None:
        state += f" exit_code={j['exit_code']}"
    tail = " (состояние обновлено после инцидента)" if j.get("state_after_as_of") else ""
    return f"Job {j.get('namespace')}/{j.get('name')}: {state}{tail}"


def apply_kg_context(ctx: Dict[str, Any], kgc: Optional[Dict[str, Any]], *,
                     include_medic: bool = True) -> Dict[str, Any]:
    """Разложить контекст графа в поля, которые читают правила.

    Добавляет, а не заменяет: живой снимок K8sFacts (его пайплайн накладывает
    позже) дополняет граф, а не стирает. `include_medic=False` — без
    источника squad-medic (сравнение в датасете).
    """
    from app.diagnostics.rules.base import same_workload

    if not isinstance(kgc, dict) or kgc.get("schema") != SCHEMA:
        return ctx
    ctx[CTX_KEY] = kgc
    service = ctx.get("service")
    # Target: у алерта нет сервиса, или сервис — метрика-источник, или алерт
    # шумовой без rollout-а — тогда «больной» workload графа честнее.
    targets = kgc.get("targets") or []
    if targets and (not service or service in NOT_TARGETS
                    or (ctx.get("alertname") in NOISE_ALERTS and not ctx.get("pod"))):
        ctx["service"] = targets[0]["workload"]
        ctx["service_from"] = "kg_targets"
    target = ctx.get("pod") or ctx.get("service")

    events = [{"type": e.get("type"), "reason": e.get("reason"), "message": e.get("message"),
               "count": e.get("count"), "pod_name": e.get("pod"),
               "namespace": e.get("namespace"),
               "last_timestamp": e.get("last_seen") or e.get("first_seen"),
               "source": "kg_pod_events"}
              for e in kgc.get("pod_events") or []]
    medic_facts: List[str] = []
    if include_medic:
        for o in kgc.get("medic_observations") or []:
            medic_facts.extend(f for f in o.get("facts") or [] if f not in medic_facts)
        events += [{**e, "source": MEDIC_PROVENANCE} for e in observation_events(medic_facts)]
    if events:
        ctx["k8s_events"] = list(ctx.get("k8s_events") or []) + events

    # Текстовые правила (crashloop, oom, process_crash) ищут сигнал в тексте;
    # только события target-а — BackOff соседнего сервиса сквада иначе стал
    # бы уверенным фактом этого инцидента (structured k8s_events привязывает
    # PodEventsRule сам).
    if target:
        _block(ctx, "k8s_summary", "kg_pod_events",
               [_event_line(e) for e in kgc.get("pod_events") or []
                if same_workload(e.get("pod") or "", target)])
    jobs = kgc.get("jobs") or []
    if jobs:
        ctx["kg_jobs"] = jobs
        _block(ctx, "k8s_summary", "kg_k8s_jobs",
               [_job_line(j) for j in jobs if (j.get("failed") or 0) > 0])
    _block(ctx, "k8s_summary", MEDIC_PROVENANCE, medic_facts)
    _block(ctx, "logs_summary", "kg_log_observations",
           [f"{lg.get('level')} x{lg.get('count')} {lg.get('namespace')}/{lg.get('app')}: "
            f"{lg.get('message')}" for lg in kgc.get("logs") or []])

    deploys = kgc.get("deployments") or {}
    code = deploys.get("code") or []
    if code and not ctx.get("recent_deployments"):
        # В правило recent_deploy — только деплои кода, скоуп — сквад.
        ctx["recent_deployments"] = [
            {"name": d.get("service") or d.get("buildtype_id") or "deploy",
             "ts": d.get("finished_at") or d.get("started_at"), "status": d.get("status"),
             "buildtype_id": d.get("buildtype_id"), "number": d.get("number"),
             "sha": d.get("sha"), "namespace": d.get("namespace"),
             "attribution_scope": ("service" if d.get("service") == service
                                   and d.get("attribution_scope") == "service"
                                   else "namespace")}
            for d in code
        ]
    ctx["kg_squad_alerts"] = [a for a in kgc.get("alerts") or [] if not a.get("noise_kinds")]

    status = dict(ctx.get("source_status") or {})
    for src, fields in _SOURCE_FIELDS.items():
        st = (kgc.get("sources") or {}).get(src) or {}
        if st.get("status") == SourceStatus.FAILED.value:
            for f in fields:
                status.setdefault(f, st.get("reason") or f"{src} недоступен")
    for f in ("k8s_events", "recent_deployments"):
        if ctx.get(f) and f not in status:
            status[f] = PARTIAL_REASON
    ctx["source_status"] = status
    return ctx


# --- текст для модели -----------------------------------------------------------


def kg_context_prompt(kgc: Optional[Dict[str, Any]], *, include_medic: bool = True,
                      max_events: int = 15) -> str:
    """Что граф знает о стенде на момент инцидента — блоком в промпт модели.

    Только наблюдаемое и история; каждая строка подписана источником. Модель
    видит тот же граф, что и правила, а не один алерт.
    """
    if not isinstance(kgc, dict) or kgc.get("schema") != SCHEMA:
        return ""
    lines = [f"=== KNOWLEDGE GRAPH CONTEXT (as of {kgc.get('as_of')}, "
             f"scope {kgc.get('ns_scope')}) ==="]
    targets = kgc.get("targets") or []
    if targets:
        lines.append("Most affected workloads: " + "; ".join(
            f"{t['namespace']}/{t['workload']} ({', '.join(t['reasons'])})" for t in targets[:3]))
    evs = [e for e in kgc.get("pod_events") or [] if e.get("reason") in BAD_POD_REASONS]
    if evs:
        lines.append("[kg_pod_events] bad pod events in window:")
        lines += [f"  {e.get('namespace')}/{e.get('pod')}: {_event_line(e)}"
                  for e in evs[:max_events]]
    failed_jobs = [j for j in kgc.get("jobs") or [] if (j.get("failed") or 0) > 0]
    if failed_jobs:
        lines.append("[kg_k8s_jobs] failed jobs:")
        lines += [f"  {_job_line(j)}" for j in failed_jobs[:10]]
    d = kgc.get("deployments") or {}
    if d.get("code"):
        lines.append("[kg_deployments] code deploys in window:")
        lines += [f"  {x.get('namespace')}/{x.get('service')} {x.get('buildtype_id')} "
                  f"#{x.get('number') or '?'} {x.get('status') or ''} at {x.get('started_at')}"
                  for x in d["code"][:5]]
    if d.get("rollouts") or d.get("statics_count"):
        lines.append(f"[kg_deployments] k8s rollouts: {len(d.get('rollouts') or [])}, "
                     f"statics rebuilds (not code): {d.get('statics_count') or 0}")
    alerts = [a for a in kgc.get("alerts") or [] if not a.get("noise_kinds")]
    if alerts:
        lines.append("[kg_alerts] squad alerts in window: " + ", ".join(
            sorted({f"{a.get('alertname')}@{a.get('service')}" for a in alerts})[:12]))
    if include_medic:
        facts: List[str] = []
        for o in kgc.get("medic_observations") or []:
            facts.extend(f for f in o.get("facts") or [] if f not in facts)
        if facts:
            lines.append(f"[{MEDIC_PROVENANCE}] observed on the stand (observations only, "
                         "no conclusions):")
            lines += [f"  - {f}" for f in facts]
    if kgc.get("logs"):
        lines.append("[kg_log_observations] errors:")
        lines += [f"  {lg.get('level')} x{lg.get('count')} {lg.get('app')}: {lg.get('message')}"
                  for lg in kgc["logs"][:5]]
    hist = kgc.get("incident_history") or []
    if hist:
        lines.append("[kg_incidents] history (same squad, prior days): " + "; ".join(
            f"{h.get('alertname')}@{h.get('service')} x{h.get('count')}"
            + (f" ({h['noise']} noise)" if h.get("noise") else "") for h in hist[:6]))
    rem = kgc.get("remediation_history") or []
    if rem:
        lines.append("[kg_remediation_events] earlier remediation runs:")
        for h in rem[:5]:
            fixed = ("stand healthy after" if h.get("fixed_semantics") == "healthy"
                     else "applied something") if h.get("fixed") else "not fixed"
            line = f"  {h.get('started_at')} {h.get('actor')}: outcome={h.get('outcome')}, {fixed}"
            if h.get("root_cause"):
                line += f"; its conclusion: {h['root_cause']}"
            lines.append(line)
    failed = [n for n, s in (kgc.get("sources") or {}).items()
              if s.get("status") == SourceStatus.FAILED.value]
    if failed:
        lines.append("Unavailable graph sources (unknown, not absent): " + ", ".join(failed))
    return "\n".join(lines) if len(lines) > 1 else ""


def service_pod_events(kgc: Optional[Dict[str, Any]], namespace: str, service: Optional[str],
                       *, around: datetime, window_minutes: int = 60,
                       limit: int = 5) -> Optional[List[Dict[str, Any]]]:
    """События подов сервиса из контекста графа — в форме
    `queries.recent_pod_events_for` (её читает рендер эмбеда).

    None — контекста нет или источник событий упал: вызывающий идёт в граф
    сам, как раньше. Пустой список — «опрошено, пусто».
    """
    if not isinstance(kgc, dict) or kgc.get("schema") != SCHEMA or not service:
        return None
    st = (kgc.get("sources") or {}).get("kg_pod_events") or {}
    if st.get("status") == SourceStatus.FAILED.value:
        return None
    around_a = _aware(around) or datetime.now(timezone.utc)
    lo = around_a - timedelta(minutes=window_minutes)
    hi = around_a + timedelta(minutes=window_minutes)
    rows = []
    for e in kgc.get("pod_events") or []:
        if e.get("namespace") != namespace or e.get("service") != service:
            continue
        first, last = _aware(e.get("first_seen")), _aware(e.get("last_seen"))
        if first is None:
            continue
        last = last or first
        if last < lo or first > hi:
            continue
        rows.append((last, first, e))
    rows.sort(key=lambda t: t[0], reverse=True)
    out: List[Dict[str, Any]] = []
    for last, first, e in rows[:limit]:
        out.append({
            "reason": e.get("reason"),
            "pod_name": e.get("pod"),
            "first_seen": _naive(first),
            "last_seen": _naive(last),
            "count": e.get("count"),
            "minutes_before": int((around_a - first).total_seconds() // 60),
            "minutes_since_last": int((around_a - last).total_seconds() // 60),
            "message": (e.get("message") or "")[:200],
        })
    return out


def as_of_for(starts_at: Any, now: Optional[datetime] = None) -> datetime:
    """Момент, на который собирать контекст: начало инцидента, если оно есть
    и не в будущем, иначе — сейчас."""
    now = now or datetime.now(timezone.utc)
    t = _aware(starts_at)
    return t if t and t <= now else now


def build_kg_context(db: Any, *, namespace: Optional[str], service: Optional[str],
                     alertname: Optional[str], as_of: datetime) -> Optional[Any]:
    """Прод: собрать через сессию. None — нечего собирать (нет namespace)."""
    if not namespace or not _NS_RE.match(namespace):
        return None
    scope = KGScope(namespace=namespace, service=service, alertname=alertname, as_of=as_of)
    return KG_CONTEXT.run_sync(collect_kg_incident_context, SessionReader(db), scope)


__all__ = [
    "CTX_KEY", "KGReader", "KGScope", "KG_CONTEXT", "PsqlReader", "SCHEMA", "SessionReader",
    "apply_kg_context", "as_of_for", "build_kg_context", "collect_kg_incident_context",
    "fetch_kg_incident_context", "kg_context_prompt", "render_sql",
    "select_targets", "service_pod_events", "squad_prefix", "workload_of",
]
