"""Deterministic KG-enrichment для AlertManager-алертов.

Собирает контекст из knowledge_graph (recent_deploys, nearby_alerts,
incidents_on, downstream count, team_owner) и прогоняет правила из
app.diagnostics.rules — БЕЗ LLM-вызовов. Используется в
/webhooks/alertmanager/enrich-and-forward.

Структура `EnrichedContext` — то, что builder в discord_service
консьюмит для построения embed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import structlog
from sqlalchemy.orm import Session

from app.config import settings
from app.diagnostics.facts import Fact
from app.diagnostics.rules.recent_deploy import RecentDeployRule
from app.diagnostics.rules.upstream_degraded import UpstreamDegradedRule
from app.knowledge_graph.queries import (blast_radius_for,
                                         cluster_deploy_activity,
                                         current_replicas_from_kg,
                                         incidents_on, ingress_health_for,
                                         latest_pod_event_for,
                                         nats_impact_for, nearby_alerts,
                                         pod_event_summary_for,
                                         recent_deploys_for,
                                         recent_deploys_for_namespaces,
                                         recent_pod_events_for, upstream_of)
from app.knowledge_graph.schema import Service, ServiceEdge
from app.models.incident import Incident

log = structlog.get_logger()


def _ns_deploy_fallback_applies(namespace: str) -> bool:
    """True если ns подходит под NS-level deploy attribution (app-ns).

    Префиксы из `ENRICH_NS_DEPLOY_FALLBACK_NS_PREFIXES` (CSV). Пустая
    настройка = fallback выключен полностью.
    """
    raw = (getattr(settings, "ENRICH_NS_DEPLOY_FALLBACK_NS_PREFIXES", "") or "")
    prefixes = [p.strip() for p in raw.split(",") if p.strip()]
    return any(namespace.startswith(p) for p in prefixes)


def _cluster_sibling_prefixes(namespace: str) -> List[str]:
    """Префиксы ns, делящих физический кластер с `namespace`.

    Группы из `ENRICH_CLUSTER_NS_GROUPS` (`;` между группами, `,` между
    префиксами). namespace относится к группе, если матчит любой её
    префикс — возвращаем все префиксы этой группы (включая собственный:
    вызов исключает сам namespace отдельно). [] если namespace не в одной
    из групп → cluster-probe для него выключен.
    """
    raw = getattr(settings, "ENRICH_CLUSTER_NS_GROUPS", "") or ""
    for group in raw.split(";"):
        prefixes = [p.strip() for p in group.split(",") if p.strip()]
        if any(namespace.startswith(p) for p in prefixes):
            return prefixes
    return []


def _matter_signals(
    inbound_count_by_kind: Dict[str, int],
    team_owner: Optional[str],
    pod_events: List[Dict[str, Any]],
    recurrence_24h: List[Dict[str, Any]],
) -> List[str]:
    """Phase 3-A «Why this alert matters» — deterministic signals over
    available KG context. No LLM. Возвращает list строк (2-4 max),
    embed render берёт top-3.
    """
    out: List[str] = []
    # Inbound importance — shared dep, high blast radius
    total_inbound = sum((inbound_count_by_kind or {}).values())
    if total_inbound >= 20:
        out.append(f"🌐 **Shared dep**: {total_inbound} inbound callers — blast radius широкий")
    elif total_inbound >= 5:
        out.append(f"📍 **{total_inbound} inbound callers** зависят от этого сервиса")
    # Team criticality
    if team_owner == "platform":
        out.append("⚙️ **Infra-critical**: team_owner=`platform` — обычно shared/foundational")
    elif team_owner == "shared":
        out.append("🔗 **Shared tier**: services из этого ns обычно cross-realm")
    # Pod event severity (BackOff×1000+ — chronic)
    for pe in (pod_events or [])[:2]:
        count = pe.get("count") or 0
        if count >= 1000:
            out.append(
                f"🔥 **Хроник**: pod_event `{pe.get('reason','?')}` × {count} retries — "
                "несколько суток крашится, нужен owner"
            )
            break
    # Recurrence
    rec = len(recurrence_24h or [])
    if rec >= 5:
        out.append(f"🔁 **{rec} раз за 24h** — повторяющийся pattern, не одноразовое")
    return out[:3]


@dataclass
class EnrichedContext:
    """Готовая структура для рисовалки Discord-embed.

    Все поля могут быть пустыми/None — builder обязан это переносить
    (KG может быть холодным или сервис вне graph).
    """

    incident: Incident
    service: Optional[str] = None
    pod: Optional[str] = None
    team_owner: Optional[str] = None
    in_kg: bool = False

    recent_deploys: List[Dict[str, Any]] = field(default_factory=list)
    # Скоуп recent_deploys: "service" — деплои самого сервиса (как было
    # всегда); "namespace" — NS-level fallback для алертов без резолва
    # сервиса (PreprodRestartsSpike и т.п.): деплои ЛЮБОГО сервиса в ns
    # алерта за ns_deploy_window_min. Рендер для namespace-скоупа другой:
    # отдельное поле «Deploy-связь» с явным вердиктом, включая негативный
    # («деплоев не было — вряд ли связано с деплоем»).
    deploy_scope: str = "service"
    ns_deploy_window_min: Optional[int] = None
    # Cross-namespace deploy collateral (инцидент ProdEndpointDown
    # 2026-06-15): когда в ns алерта деплоя нет, но в СОСЕДНИХ ns того же
    # кластера за окно шёл bulk-rollout (image-pull/CRI pressure роняет
    # соседей). Заполняется только в NS-fallback ветке при пустом
    # recent_deploys. Структура — см. queries.cluster_deploy_activity.
    # Рендерится в поле «Deploy-связь»: вместо ложного «деплоев не было —
    # вряд ли связано» показываем cross-namespace collateral-гипотезу.
    cluster_deploy_activity: Dict[str, Any] = field(default_factory=dict)
    upstream_alerts: List[Dict[str, Any]] = field(default_factory=list)
    recurrence_24h: List[Dict[str, Any]] = field(default_factory=list)
    # Inbound: сколько сервисов вызывают/зависят от этого. Раньше поле
    # называлось `downstream_count_by_kind` — это была семантическая ошибка
    # (граф: src→dst, edges с dst=svc означают "кто меня вызывает" =
    # inbound callers, не downstream).
    inbound_count_by_kind: Dict[str, int] = field(default_factory=dict)
    # Outgoing: куда сервис сам ходит (edges с src=svc). Это «зависимости» —
    # для leaf-сервисов это самая важная диагностика при падении.
    outgoing_deps: List[Dict[str, Any]] = field(default_factory=list)
    # Pod-events (kg_pod_events) — k8s diagnostic signal в окне инцидента.
    pod_events: List[Dict[str, Any]] = field(default_factory=list)
    # UX polish (on-call feedback 10:38): конкретный pod-name, последний
    # containerStatus.reason, current ready/desired — чтобы on-call видел
    # «какой pod, что с ним, сколько реплик жилых».
    pod_name: Optional[str] = None
    container_reason: Optional[str] = None
    replicas_ready_desired: Optional[str] = None  # "1/3" — sentinel для render-а
    # A6 (Phase 2): Jira-issues linkback. Тикеты с label=backend и service
    # в summary за последние JIRA_SEARCH_DAYS дней. Embed-секция «Ticketsy»
    # с прямыми URL на issue.
    jira_issues: List[Dict[str, Any]] = field(default_factory=list)

    rule_facts: List[Fact] = field(default_factory=list)

    # rollout-noise — выставляется heuristic-ом в enrich_alert ниже
    rollout_noise: bool = False

    # meta-noise — alert является meta-агрегатом (`*NewCriticalAlerts`) или
    # производным control-plane scrape-gap. Выставляется в enrich_alert.
    # Render: grey + 🔇, без 🚨/@mention; карточка остаётся видимой.
    meta_noise: bool = False

    # gen-mismatch-noise — KubeDeploymentGenerationMismatch при ЗДОРОВЫХ
    # репликах (ready==desired): observedGeneration отстаёт из-за churn-а
    # аннотаций внешним контроллером, накат сошёлся. Условный (см.
    # _detect_gen_mismatch_noise): при ready<desired остаётся громким.
    # Render тот же muted, что у meta_noise, тег — 🔇 GENERATION-CHURN.
    gen_mismatch_noise: bool = False

    # AM inhibit/silence: если alert.status_extra или labels указывают на
    # suppressed-состояние — заполняется human-readable строкой («🔇 silenced
    # by X», «🔇 inhibited by Y»). None — alert активен. Embed-builder
    # рисует «Status» поле + orange-color override.
    inhibition_state: Optional[str] = None

    kg_data_age_sec: Optional[int] = None

    # Wave 7 секции (только для critical-render, skip-if-empty внутри builders).
    # blast_radius — `{services, urls, services_total, urls_total}` для
    # «кто маршрутит трафик на меня» (serves_traffic IN) и «какие URL
    # затронуты» (routes_to). См. queries.blast_radius_for.
    blast_radius: Dict[str, Any] = field(default_factory=dict)
    # nats_impact — list[{subject, direction, impact_count, impact_others}],
    # отсортирован по impact_count desc. См. queries.nats_impact_for.
    nats_impact: List[Dict[str, Any]] = field(default_factory=list)
    # pod_trail — `{total, by_reason: [(reason, count), ...]}` агрегация
    # PodEvent за окно ±60м. Параллельна `pod_events` (top-5 individual),
    # фокус на counts. См. queries.pod_event_summary_for.
    pod_trail: Dict[str, Any] = field(default_factory=dict)
    # ingress_health — ingress-derived HTTP RED (5xx-rps/p95) per host/path из
    # kg_ingress_observations. Живой источник (nginx-ingress), в отличие от
    # per-service kg_service_health.http_5xx (закрыт JWT, WO-12483). Помечен
    # is_ingress_derived. См. queries.ingress_health_for.
    ingress_health: Dict[str, Any] = field(default_factory=dict)

    # Свободное поле для metadata, которая не имеет первого-класса своего слота:
    # `synthetic_fallback` (resolver hit на synthetic Service), `target_resolve_*` —
    # debug-сигналы, не для render-а в embed напрямую.
    extras: Dict[str, Any] = field(default_factory=dict)

    def primary_hypothesis(self) -> Optional[str]:
        """Берёт самый сильный observed fact для подсказки в Root Cause."""
        observed = [f for f in self.rule_facts if f.observed]
        if not observed:
            return None
        # Сортируем по confidence — берём top-1.
        top = max(observed, key=lambda f: f.confidence)
        return _fact_to_short_text(top)

    def why_this_matters(self) -> List[str]:
        """Phase 3-A: bullets «почему этот alert важен» — для embed-секции."""
        return _matter_signals(
            inbound_count_by_kind=self.inbound_count_by_kind,
            team_owner=self.team_owner,
            pod_events=self.pod_events,
            recurrence_24h=self.recurrence_24h,
        )


def _fact_to_short_text(fact: Fact) -> str:
    ev = fact.evidence or {}
    if fact.source_rule == "RecentDeployRule":
        deploys = ev.get("deploys") or []
        if deploys:
            d = deploys[0]
            mins = d.get("minutes_before_incident")
            sha = (d.get("sha") or "")[:7]
            build = d.get("number") or d.get("buildtype_id") or "?"
            triggered = d.get("triggered_by") or ""
            by = f" by {triggered}" if triggered else ""
            from app.utils.time_human import humanize_minutes_ago
            when = humanize_minutes_ago(mins)
            return f"Deploy #{build}{by} ({sha}) {when} — возможный регресс"
    if fact.source_rule == "UpstreamDegradedRule":
        cnt = ev.get("count", 0)
        alerts = ev.get("alerts") or []
        if alerts:
            first = alerts[0]
            svc = first.get("service") or "?"
            an = first.get("alertname") or "?"
            return f"Upstream `{svc}` алертит `{an}` ({cnt} cascading)"
    # Phase 3-A: PodEventsRule → reason-specific descriptions
    if fact.source_rule == "PodEventsRule":
        reason = ev.get("reason", "")
        count = ev.get("count", 1)
        message = (ev.get("message") or "")[:90]
        count_part = f" ×{count}" if count > 1 else ""
        # Pretty descriptions per FactKind
        if fact.kind == "oom_killed":
            return f"OOMKilled{count_part}: container превышает memory limit"
        if fact.kind == "crashloop":
            chronic = " (хроник 1000+ retries)" if count > 1000 else ""
            return f"k8s `{reason}`{count_part}{chronic} — pod не запускается"
        if fact.kind == "failed_scheduling":
            return f"FailedScheduling{count_part}: нет nodes под requested resources"
        if fact.kind == "resource_pressure":
            return f"`{reason}` на node — нехватка ресурсов кластера"
        return f"k8s `{reason}`{count_part}: {message}"
    if fact.source_rule == "OOMKilledRule":
        return "OOMKilled: container превышает memory limit (из logs/exit-code)"
    if fact.source_rule == "CrashLoopBackOffRule":
        return "Pod в CrashLoopBackOff — startup ошибка / зависимости недоступны"
    if fact.source_rule == "FailedSchedulingRule":
        return "FailedScheduling — нет nodes для pod (resource constraints)"
    return f"{fact.source_rule}: observed"


def _parse_starts_at(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str):
        s = raw.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _downstream_count_by_kind(db: Session, namespace: str, service_name: str) -> Dict[str, int]:
    """Сколько сервисов имеют edge → данный (calls / uses_nats и т.д.).

    Делается одним запросом по kg_service_edges, группируется в Python.
    """
    svc = (
        db.query(Service)
        .filter(Service.namespace == namespace, Service.name == service_name)
        .one_or_none()
    )
    if svc is None:
        return {}
    rows = db.query(ServiceEdge).filter(ServiceEdge.dst_id == svc.id).all()
    out: Dict[str, int] = {}
    for r in rows:
        out[r.kind] = out.get(r.kind, 0) + 1
    return out


def _kg_data_age(db: Session, namespace: str, service_name: str) -> Optional[int]:
    svc = (
        db.query(Service)
        .filter(Service.namespace == namespace, Service.name == service_name)
        .one_or_none()
    )
    if svc is None or svc.updated_at is None:
        return None
    age = datetime.now(timezone.utc) - svc.updated_at.replace(tzinfo=timezone.utc)
    return int(age.total_seconds())


# Root cause #1: `incident.namespace` для `kube_*` алёртов часто указывает
# на ns источника метрики (monitoring / kube-system), а не на target deploy.
# В результате 330 alerts/неделю всем стеком улетают в misattribute на
# `vm-kube-state-metrics`. Резолвим target из labels в порядке приоритета.
#
# Pod-hash strip: kube создаёт pod как `<deployment>-<rs-hash>-<pod-hash>`
# (rs-hash = 8..10 alnum, pod-hash = 5 alnum). При `Kube*` алёртах из labels
# берём pod и режем хвост, чтобы получить имя deployment'а.
_POD_HASH_RE = re.compile(r"^(?P<deploy>.+?)-[a-z0-9]{8,10}-[a-z0-9]{5}$")
# StatefulSet pod: `<sts>-<ordinal>` (e.g. `town-db-postgresql-0`).
_STS_POD_RE = re.compile(r"^(?P<sts>.+?)-(?P<ord>\d+)$")


def _strip_pod_hash(pod: str) -> Optional[str]:
    """Извлечь deployment-name из k8s pod-name.

    `auth-service-7f8c4b6cdf-h2x9k` → `auth-service`.
    `town-db-postgresql-0` → `town-db-postgresql` (StatefulSet ordinal).
    Не-stripped pod → None.
    """
    if not pod:
        return None
    m = _POD_HASH_RE.match(pod)
    if m:
        return m.group("deploy")
    m = _STS_POD_RE.match(pod)
    if m:
        return m.group("sts")
    return None


def _resolve_target_service_from_labels(
    labels: Dict[str, str],
) -> Tuple[Optional[str], Optional[str]]:
    """Извлечь (namespace, service_name) для target deployment'а.

    Порядок приоритета (root cause #1 fix):
        1. labels["deployment"]    — KubeDeployment*
        2. labels["statefulset"]   — KubeStatefulSet*
        3. labels["daemonset"]     — KubeDaemonSet*
        4. labels["job_name"]      — KubeJobFailed
        5. labels["pod"] → strip hash → deployment-derive
        6. labels["container"]     — last-resort fallback

    namespace ВСЕГДА из labels["namespace"] (если есть). Это namespace
    target'а, не источника метрики. Если ничего не нашли — (None, None).
    """
    if not labels:
        return (None, None)
    namespace = labels.get("namespace") or None
    # Priority chain: первое непустое поле выигрывает.
    for key in ("deployment", "statefulset", "daemonset", "job_name"):
        value = labels.get(key)
        if value:
            return (namespace, value)
    pod = labels.get("pod")
    if pod:
        derived = _strip_pod_hash(pod)
        if derived:
            return (namespace, derived)
    container = labels.get("container")
    if container:
        return (namespace, container)
    return (namespace, None)


# Blackbox-проба (ProdEndpointDown / PreprodEndpointDown) несёт СТАТИЧЕСКИЙ
# `namespace`-label (prod-shared), выбранный лишь для AM-роута. Реальный
# затронутый realm зашит в URL цели пробы (`instance`):
#   https://wo-api4-prod.lastoasisgame.com/town/health/ready → prod-kingdom4
#   https://wo-api-prod.lastoasisgame.com/auth/health/ready  → prod-shared
# Инцидент 2026-06-30: полный прод-релиз prod-280 ролльнул prod-kingdom4 и на
# время раскатки уронил пробу wo-api4; атрибуция смотрела в prod-shared,
# деплоя там «не нашла» → ложный «795 деплоев в соседях» + не подавила @here.
_PROBE_HOST_RE = re.compile(
    r"^wo-api(?P<n>\d*)-(?P<env>[a-z]+)\.lastoasisgame\.com$"
)


def _resolve_probe_namespace(labels: Dict[str, str]) -> Optional[str]:
    """Realm-namespace из URL blackbox-пробы (`instance` / `target` label).

    `wo-api<N>-<env>.lastoasisgame.com` → `<env>-kingdom<N>`;
    `wo-api-<env>...` (без номера) → `<env>-shared`.

    None — `instance` отсутствует или host не матчит prod-blackbox-паттерн
    (тогда namespace остаётся как был — из labels/incident). Принимает как
    полный URL (со схемой), так и голый host[:port][/path].
    """
    raw = (labels or {}).get("instance") or (labels or {}).get("target") or ""
    if not raw:
        return None
    host = raw
    if "://" in host:
        host = urlparse(host).hostname or ""
    else:
        # голый host — отрезаем path и port
        host = host.split("/", 1)[0].split(":", 1)[0]
    m = _PROBE_HOST_RE.match(host.strip().lower())
    if not m:
        return None
    env = m.group("env")
    n = m.group("n")
    return f"{env}-kingdom{n}" if n else f"{env}-shared"


def _inhibition_state(alert_payload: Any) -> Optional[str]:
    """Возвращает human-readable строку про silence/inhibit состояние alert-а,
    или None если alert не suppressed.

    Принимает либо `Incident` (через .status_extra / labels), либо raw dict
    в AM-payload-формате (`{"status": {...}, "labels": {...}}`). Это позволяет
    звать как из enrichment-пути (Incident), так и из webhook-handler-а
    (decision до построения Incident).

    Возвращаемая строка — для embed-секции «Status», например:
      "🔇 silenced (id: abc12345) · expires soon"
      "🔇 inhibited by KubePodCrashLooping (fp 4f3a...)"
      "🔇 silenced & inhibited"
    None — alert активен / не AM-suppressed.
    """
    extra: Optional[Dict[str, Any]] = None
    labels: Dict[str, str] = {}
    if isinstance(alert_payload, dict):
        s = alert_payload.get("status")
        if isinstance(s, dict):
            extra = s
        else:
            extra = alert_payload.get("status_extra")
        labels = alert_payload.get("labels") or {}
    else:
        # Incident-shaped
        extra = getattr(alert_payload, "status_extra", None)
        labels = getattr(alert_payload, "labels", {}) or {}

    if not extra:
        # labels-based fallback — некоторые шлюзы пишут это прямо в labels.
        sb = labels.get("silenced_by")
        ib = labels.get("inhibited_by")
        if not sb and not ib:
            return None
        extra = {
            "state": "suppressed",
            "silencedBy": [sb] if sb else [],
            "inhibitedBy": [ib] if ib else [],
        }

    state = (extra.get("state") or "").lower()
    if state not in ("suppressed", "silenced", "inhibited"):
        return None

    silenced_by = extra.get("silencedBy") or extra.get("silenced_by") or []
    inhibited_by = extra.get("inhibitedBy") or extra.get("inhibited_by") or []
    # Нормализуем в list-of-str (бывает single str или None в нестрогих payload).
    if isinstance(silenced_by, str):
        silenced_by = [silenced_by]
    if isinstance(inhibited_by, str):
        inhibited_by = [inhibited_by]

    parts: List[str] = []
    if silenced_by:
        sid = str(silenced_by[0])[:8]
        more = f" (+{len(silenced_by)-1})" if len(silenced_by) > 1 else ""
        parts.append(f"silenced (id: `{sid}`){more}")
    if inhibited_by:
        fp = str(inhibited_by[0])
        # Если это похоже на alertname — оставляем как есть. Иначе trim hash.
        if not re.match(r"^[A-Z][a-zA-Z]+$", fp):
            fp = fp[:8]
        more = f" (+{len(inhibited_by)-1})" if len(inhibited_by) > 1 else ""
        parts.append(f"inhibited by `{fp}`{more}")
    if not parts:
        # state=suppressed без явных причин — всё равно сигнал, показываем.
        return "🔇 suppressed (no reason)"
    return "🔇 " + " · ".join(parts)


def _detect_rollout_noise(incident: Incident, recent_deploys: List[Dict[str, Any]]) -> bool:
    """Heuristic: KubeDeploymentGenerationMismatch + deploy <5 мин назад → noise."""
    alertname = incident.labels.get("alertname", "")
    if alertname not in {"KubeDeploymentGenerationMismatch", "KubeReplicaSetMismatch"}:
        return False
    for d in recent_deploys:
        if d.get("minutes_before_incident", 999) <= 5:
            return True
    return False


# Meta-агрегаты и scrape-plumbing — шумные ВСЕГДА (в отличие от rollout-noise,
# не привязаны к окну деплоя). См. config.META_NOISE_ENABLED.
META_NOISE_ALERTNAMES = frozenset({
    "ProdNewCriticalAlerts",     # агрегат-счётчик новых критикалов
    "etcdInsufficientMembers",   # control-plane scrape-gap (CP на 127.0.0.1)
    "ScrapePoolHasNoTargets",    # scrape-gap
    "RecordingRulesNoData",      # scrape-gap
})


def _detect_meta_noise(incident: Incident) -> bool:
    """True если alert — meta-агрегат или scrape-plumbing шум.

    Безопасно приглушать: `*NewCriticalAlerts` дублирует сигнал (каждый
    реальный критикал приходит копайлоту отдельной карточкой со своим
    сервисом/деплоем/KG), а etcd/scrape/recording-rule алёрты — известный
    control-plane scrape-gap (не инцидент). Не дропаем, а помечаем для
    muted-render-а (grey + 🔇, без 🚨/@mention).
    """
    alertname = (incident.labels or {}).get("alertname", "")
    if not alertname:
        return False
    if alertname in META_NOISE_ALERTNAMES:
        return True
    # Семейство `<Env>NewCriticalAlerts` (Prod/Preprod/Squad/...) — все агрегаты.
    return alertname.endswith("NewCriticalAlerts")


def _detect_gen_mismatch_noise(
    incident: Incident, replicas_ready_desired: Optional[str]
) -> bool:
    """True если KubeDeploymentGenerationMismatch — доброкачественный churn.

    metadata.generation != observedGeneration штатно флапает, когда внешний
    контроллер (Rancher/cattle-cluster-agent дописывает publicEndpoints-
    аннотацию) бьёт generation, а deployment-контроллер на миг отстаёт —
    накат при этом давно сошёлся. Тот же alertname, однако, сигналит и
    реальный зависший накат.

    Различаем по здоровью реплик (ctx.replicas_ready_desired, формат
    "ready/desired"): приглушаем ТОЛЬКО когда ready==desired и ready>=1.
    Любая неоднозначность — ready<desired, "?/N", None, нераспарсилось —
    оставляет alert ГРОМКИМ (fail-safe loud: на проде лучше лишний пинг, чем
    проспать зависший накат). Не дропаем — помечаем для muted-render-а.
    """
    alertname = (incident.labels or {}).get("alertname", "")
    if alertname != "KubeDeploymentGenerationMismatch":
        return False
    if not replicas_ready_desired or "/" not in replicas_ready_desired:
        return False  # нет данных о репликах → fail-safe loud
    ready_str, _, desired_str = replicas_ready_desired.partition("/")
    try:
        ready = int(ready_str)
        desired = int(desired_str)
    except ValueError:
        return False  # "?/N" и прочее нераспарсиваемое → fail-safe loud
    # Сошедшийся накат: все желаемые реплики живы и их хотя бы одна.
    return ready >= 1 and ready == desired


def enrich_alert(db: Session, incident: Incident) -> EnrichedContext:
    """Главная точка — синхронный, без LLM, ~5 SQL-запросов.

    Безопасно при пустом KG — каждое поле fallback в пустое значение.
    """
    labels = incident.labels or {}
    pod = labels.get("pod")
    # Root cause #1: target из labels в priority order. incident.service_name
    # (если бы он был) сейчас глючит — приоритет за labels-resolved. Старый
    # код брал `service || deployment` → пропускал KubeStatefulSet/Daemon/Job
    # alerts и игнорировал pod-hash-strip. Misattribute на vm-kube-state-metrics
    # =  330 alerts/week просачивались в #infra-error именно из-за этого.
    resolved_ns, resolved_svc = _resolve_target_service_from_labels(labels)
    # Если labels пустые/не дали target — fallback на incident.namespace.
    # Это уже не сервис-namespace, а namespace источника метрики (часто
    # `monitoring`), но без неё мы вообще ничего не найдём в KG.
    namespace = resolved_ns or incident.namespace or ""
    service = resolved_svc
    # Legacy fallback: старые alerts с `service`/`app` label без структурных
    # `deployment`/`pod` (например, custom Prometheus rules). Оставляем
    # last-resort, чтобы не регрессировать на не-Kube alerts.
    if not service:
        service = labels.get("service") or labels.get("app")

    # Blackbox-проба: статический `namespace`-label (prod-shared) — лишь
    # AM-роут-метка, реальный realm в URL `instance`. Переопределяем namespace
    # на host-derived prod-kingdom<N>, чтобы deploy-атрибуция (NS-fallback ниже)
    # смотрела в правильный ns. Только для service-less probe-алертов — у
    # Kube-алертов с резолвенным сервисом instance не трогаем.
    probe_ns_from: Optional[str] = None
    if not service and getattr(settings, "ENRICH_PROBE_NS_RESOLVE_ENABLED", True):
        probe_ns = _resolve_probe_namespace(labels)
        if probe_ns and probe_ns != namespace:
            probe_ns_from = namespace
            namespace = probe_ns

    ctx = EnrichedContext(
        incident=incident,
        service=service,
        pod=pod,
        inhibition_state=_inhibition_state(incident),
    )
    if probe_ns_from is not None:
        # debug-сигнал: какой ns был в label и куда срезолвили из URL пробы
        ctx.extras["probe_ns_resolved"] = namespace
        ctx.extras["probe_ns_static_label"] = probe_ns_from
        log.info(
            "enrich.probe_ns_resolved",
            static_label=probe_ns_from, resolved=namespace,
            instance=(labels.get("instance") or labels.get("target")),
        )

    if not namespace or not service:
        # NS-level deploy attribution (запрос on-call 2026-06-10): сервис
        # не резолвится (namespace-агрегаты вроде PreprodRestartsSpike), но
        # triage-вопрос «деплой или нет» отвечается деплоями всего ns.
        # Только для app-namespace'ов — деплои в monitoring/kube-system
        # нерелевантны источнику таких алертов.
        if namespace and _ns_deploy_fallback_applies(namespace):
            window_min = settings.ENRICH_DEPLOY_LOOKBACK_MIN
            ctx.deploy_scope = "namespace"
            ctx.ns_deploy_window_min = window_min
            try:
                incident_at = _parse_starts_at(incident.starts_at)
                now = datetime.now(timezone.utc)
                starts_age_hours = (now - incident_at).total_seconds() / 3600
                effective_at = now if starts_age_hours > 24 else incident_at
                ctx.recent_deploys = recent_deploys_for_namespaces(
                    db, [namespace], before=effective_at,
                    lookback_minutes=window_min,
                )
                # В своём ns деплоя нет — но мог быть bulk-rollout в
                # соседних ns кластера (cross-namespace collateral).
                # Иначе атрибуция врёт «не было — вряд ли связано».
                if not ctx.recent_deploys:
                    siblings = _cluster_sibling_prefixes(namespace)
                    if siblings:
                        ctx.cluster_deploy_activity = cluster_deploy_activity(
                            db, sibling_prefixes=siblings,
                            exclude_namespace=namespace,
                            before=effective_at, lookback_minutes=window_min,
                        )
            except Exception as e:
                log.warning("enrich.ns_deploy_fallback_failed", error=str(e))
        else:
            log.debug("enrich.skip_no_service", namespace=namespace, service=service)
        return ctx

    incident_at = _parse_starts_at(incident.starts_at)
    # Точка роста #1: для длительных хроник (alert firing 3+ суток —
    # bot-service например) `before=incident.starts_at` указывает на
    # «когда впервые зафайрило», а это может быть 14 мая. Для current
    # embed более релевантно «recent deploys before now», а не «before
    # incident_at_3_days_ago». Используем max(starts_at, now-30д) как
    # adaptive окно: для свежих alerts (часы) — как было, для хроник
    # (дни) — relative к now.
    now = datetime.now(timezone.utc)
    starts_age_hours = (now - incident_at).total_seconds() / 3600
    effective_at = now if starts_age_hours > 24 else incident_at

    # 1. Recent deploys: сначала узкое окно (для regression-сигнала
    # «deploy за N минут до alert-а»), если пусто — расширяем до 7 дней,
    # чтобы embed всё равно показал последние deploys (для редко-катящихся
    # сервисов 60-мин окно почти всегда пустое).
    try:
        ctx.recent_deploys = recent_deploys_for(
            db, namespace, service, before=effective_at,
            lookback_minutes=settings.ENRICH_DEPLOY_LOOKBACK_MIN,
        )
        if not ctx.recent_deploys:
            ctx.recent_deploys = recent_deploys_for(
                db, namespace, service, before=effective_at,
                lookback_minutes=7 * 24 * 60,  # 7 дней fallback
            )
    except Exception as e:
        log.warning("enrich.recent_deploys_failed", error=str(e))

    # 2. Upstream alerts (±15 мин)
    try:
        ctx.upstream_alerts = nearby_alerts(
            db, namespace, service, around=incident_at,
            window_minutes=settings.ENRICH_UPSTREAM_WINDOW_MIN,
        )
    except Exception as e:
        log.warning("enrich.nearby_alerts_failed", error=str(e))

    # 3. Recurrence (24h окно)
    try:
        ctx.recurrence_24h = incidents_on(
            db, namespace, service,
            since=incident_at - timedelta(minutes=settings.ENRICH_RECURRENCE_LOOKBACK_MIN),
            until=incident_at,
        )
    except Exception as e:
        log.warning("enrich.incidents_on_failed", error=str(e))

    # 4. Inbound: кто вызывает этот сервис (по kind).
    try:
        ctx.inbound_count_by_kind = _downstream_count_by_kind(db, namespace, service)
    except Exception as e:
        log.warning("enrich.inbound_count_failed", error=str(e))

    # 4b. Outgoing dependencies: куда сервис сам ходит. Для leaf-сервисов
    # это главная диагностика «упал — потому что зависит от X». Fresh-only
    # 30 дней — отсекает stale edges (см. C1 last_seen_at).
    try:
        ctx.outgoing_deps = upstream_of(
            db, namespace, service, fresh_only_days=30,
        )
    except Exception as e:
        log.warning("enrich.outgoing_deps_failed", error=str(e))

    # 4d. A6: Jira-issues linkback. Сервисная корреляция через label+summary
    # search. Только если Jira настроен (JIRA_BASE_URL+EMAIL+TOKEN+PROJECT).
    # Не обязательная зависимость — silent fail OK.
    try:
        if (settings.JIRA_BASE_URL and settings.JIRA_EMAIL
                and settings.JIRA_API_TOKEN and settings.JIRA_PROJECT_KEY):
            from app.context.jira_client import JiraClient
            jira = JiraClient(
                base_url=settings.JIRA_BASE_URL,
                email=settings.JIRA_EMAIL,
                api_token=settings.JIRA_API_TOKEN,
                project_key=settings.JIRA_PROJECT_KEY,
                backend_label=settings.JIRA_BACKEND_LABEL,
            )
            ctx.jira_issues = jira.search_by_service_sync(
                service=service,
                namespace=namespace,
                days=settings.JIRA_SEARCH_DAYS,
            )
    except Exception as e:
        log.warning("enrich.jira_search_failed", error=str(e))

    # 4c. Recent pod events (kg_pod_events) — k8s diagnostic signal.
    # Сначала узкое окно 60м (для свежих CrashLoop / OOMKilled / Unhealthy
    # как причины текущего alert-а). Если пусто — расширяем до 7д fallback,
    # чтобы embed для длительных хроник всё равно показывал последние k8s
    # события. effective_at (точка роста #1) — same adapt для хроник.
    try:
        ctx.pod_events = recent_pod_events_for(
            db, namespace, service, around=effective_at,
            window_minutes=60, limit=5,
        )
        if not ctx.pod_events:
            ctx.pod_events = recent_pod_events_for(
                db, namespace, service, around=effective_at,
                window_minutes=7 * 24 * 60, limit=5,
            )
    except Exception as e:
        log.warning("enrich.pod_events_failed", error=str(e))

    # 4e. Wave 7 enrichment: blast radius / NATS impact / pod trail.
    # Все три — best-effort, silent fail. Render в embed только при
    # severity=critical (gate в send_enriched_alert), плюс skip-if-empty
    # внутри builders. Здесь просто заполняем структуру.
    is_critical = (incident.severity or "").lower() == "critical"
    if is_critical:
        try:
            ctx.blast_radius = blast_radius_for(db, namespace, service, top_n=3)
        except Exception as e:
            log.warning("enrich.blast_radius_failed", error=str(e))
        try:
            ctx.nats_impact = nats_impact_for(db, namespace, service, top_n=3)
        except Exception as e:
            log.warning("enrich.nats_impact_failed", error=str(e))
        try:
            ctx.pod_trail = pod_event_summary_for(
                db, namespace, service, around=effective_at, window_minutes=60,
            )
        except Exception as e:
            log.warning("enrich.pod_trail_failed", error=str(e))
        try:
            ctx.ingress_health = ingress_health_for(
                db, namespace, service, window_minutes=15, top_n=3,
            )
        except Exception as e:
            log.warning("enrich.ingress_health_failed", error=str(e))

    # 4c-bis (on-call UX): конкретный pod_name + containerStatus.reason
    # из последнего kg_pod_events. Если оконные fallback пусты — берём
    # вообще latest event (latest_pod_event_for). Заполняем
    # ctx.pod_name / ctx.container_reason — embed-render их рисует
    # отдельными полями.
    try:
        latest_ev: Optional[Dict[str, Any]] = None
        if ctx.pod_events:
            # head(pod_events) уже отсортирован по first_seen DESC
            latest_ev = ctx.pod_events[0]
        else:
            latest_ev = latest_pod_event_for(db, namespace, service)
        if latest_ev:
            ctx.pod_name = latest_ev.get("pod_name") or None
            ctx.container_reason = latest_ev.get("reason") or None
    except Exception as e:
        log.warning("enrich.latest_pod_event_failed", error=str(e))

    # 4c-ter (on-call UX): replicas ready/desired. Сначала KG metadata_json
    # (дёшево), при отсутствии — live k8s API под флагом
    # INCLUDE_LIVE_K8S_STATE с hard timeout. На один embed — один лук-ап.
    try:
        rep = current_replicas_from_kg(db, namespace, service)
        if rep is None and getattr(settings, "INCLUDE_LIVE_K8S_STATE", True):
            kind_hint = None
            if labels.get("statefulset"):
                kind_hint = "statefulset"
            elif labels.get("deployment"):
                kind_hint = "deployment"
            # Локальный импорт — модуль тянет kubernetes-client, не хотим
            # утаскивать его в чистые dry-run пути (тесты с mock-db).
            try:
                from app.context.deployments import fetch_live_replicas
                rep = fetch_live_replicas(
                    namespace, service,
                    kind_hint=kind_hint,
                    timeout_sec=getattr(settings, "LIVE_K8S_TIMEOUT_SEC", 3.0),
                )
            except Exception as e:
                log.warning("enrich.live_replicas_import_failed", error=type(e).__name__)
        if rep:
            ready = rep.get("ready")
            desired = rep.get("desired")
            if ready is not None and desired is not None:
                ctx.replicas_ready_desired = f"{ready}/{desired}"
            elif desired is not None:
                ctx.replicas_ready_desired = f"?/{desired}"
    except Exception as e:
        log.warning("enrich.replicas_lookup_failed", error=str(e))

    # TODO INCLUDE_LAST_LOG_LINE: при settings.INCLUDE_LAST_LOG_LINE
    # подтянуть последнюю строку pod-логов + exit_code через
    # fetch_last_log_line(namespace, ctx.pod_name). Сейчас отключено —
    # read_namespaced_pod_log дорогой и flaky, см. on-call note item 5.

    # 5. Service metadata (team_owner, in_kg flag, data freshness)
    # Сначала ищем не-synthetic; synthetic-only попадание помечаем в extras —
    # это полезный сигнал «edge case: cron-job или nats-tool», но точно не
    # реальный target deployment.
    try:
        q = db.query(Service).filter(
            Service.namespace == namespace, Service.name == service
        )
        svc = q.filter(Service.synthetic == False).first()  # noqa: E712
        if svc is None:
            svc = q.first()
            if svc is not None:
                ctx.extras["synthetic_fallback"] = True
        if svc is not None:
            ctx.in_kg = True
            ctx.team_owner = svc.team_owner
            if svc.updated_at is not None:
                age = datetime.now(timezone.utc) - svc.updated_at.replace(tzinfo=timezone.utc)
                ctx.kg_data_age_sec = int(age.total_seconds())
    except Exception as e:
        log.warning("enrich.service_lookup_failed", error=str(e))

    # 6. Rule-based hypotheses — без LLM. Передаём в их интерфейс
    #    `recent_deployments` и `upstream_alerts`, как ожидают rules.
    rule_ctx: Dict[str, Any] = {
        "incident": incident.model_dump(),
        "namespace": namespace,
        "service": service,
        "pod": pod,
        "alertname": incident.labels.get("alertname", ""),
        "description": incident.description or "",
        "recent_deployments": ctx.recent_deploys,
        "upstream_alerts": ctx.upstream_alerts if ctx.upstream_alerts else None,
        "incident_starts_at": incident_at,
        # Phase 3-A: pod_events для PodEventsRule (mapping reason→FactKind).
        # OOMKilled / CrashLoop / FailedScheduling — это и есть deterministic
        # «most likely cause» для AM-based alerts типа KubePodCrashLooping.
        "k8s_events": ctx.pod_events,
    }
    try:
        ctx.rule_facts.extend(RecentDeployRule().evaluate(rule_ctx))
    except Exception as e:
        log.warning("enrich.recent_deploy_rule_failed", error=str(e))
    try:
        ctx.rule_facts.extend(UpstreamDegradedRule().evaluate(rule_ctx))
    except Exception as e:
        log.warning("enrich.upstream_rule_failed", error=str(e))
    # Phase 3-A: PodEventsRule даёт «причина из k8s» (OOM/CRASH/SCHED/...).
    # Это самый сильный root-cause signal на live alerts.
    try:
        from app.diagnostics.rules.pod_events import PodEventsRule
        ctx.rule_facts.extend(PodEventsRule().evaluate(rule_ctx))
    except Exception as e:
        log.warning("enrich.pod_events_rule_failed", error=str(e))

    # 7. Rollout-noise heuristic — `KubeDeploymentGenerationMismatch` сразу
    # после деплоя обычно безобиден (rollout в процессе).
    ctx.rollout_noise = _detect_rollout_noise(incident, ctx.recent_deploys)

    # 8. Meta-noise — агрегаты (`*NewCriticalAlerts`) и control-plane
    # scrape-gap. Приглушаем render, не дропаем (см. _detect_meta_noise).
    ctx.meta_noise = (
        _detect_meta_noise(incident)
        if getattr(settings, "META_NOISE_ENABLED", True)
        else False
    )

    # 9. Gen-mismatch-noise — KubeDeploymentGenerationMismatch при здоровых
    # репликах = churn observedGeneration, а не зависший накат (прецедент
    # prod-kingdom7 2026-06-23). Условный, health-gated (см.
    # _detect_gen_mismatch_noise). Не перекрываем rollout_noise (тот уже
    # глушит deploy-window кейс) — этот ловит no-deploy churn.
    if not ctx.rollout_noise and getattr(
        settings, "GEN_MISMATCH_NOISE_ENABLED", True
    ):
        ctx.gen_mismatch_noise = _detect_gen_mismatch_noise(
            incident, ctx.replicas_ready_desired
        )

    return ctx
