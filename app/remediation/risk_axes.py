"""8 risk axes для remediation decision.

Discrete enums (не float), чтобы policy YAML мог сравнивать значения по
именам без диапазонов. Каждая axis — отдельный enum со строго ограниченным
набором значений.

Axis списком из плана (memory/project_remediation_pipeline_plan.md):
1. `namespace_tier` (dev/squad/preprod/prod/system)
2. `resource_kind` (pod/deployment/job/statefulset/pvc/secret)
3. `blast_radius` (none/low/medium/high)
4. `data_plane` (no/maybe/yes)
5. `freshness` (fresh/chronic/stale)
6. `confidence` (weak/medium/strong)
7. `reversibility` (easy/partial/hard)
8. `idempotency` (safe/guarded/unsafe)
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Mapping


class NamespaceTier(str, Enum):
    """Иерархия namespace-ов по чувствительности к remediation.

    Mapping строится из префикса/постфикса ns в `_classify_namespace`. Любой
    неизвестный prefix падает в `system` (наиболее ограничительный — block).
    """
    DEV = "dev"
    SQUAD = "squad"
    PREPROD = "preprod"
    PROD = "prod"
    SYSTEM = "system"


class ResourceKind(str, Enum):
    POD = "pod"
    DEPLOYMENT = "deployment"
    JOB = "job"
    STATEFULSET = "statefulset"
    PVC = "pvc"
    SECRET = "secret"
    UNKNOWN = "unknown"


class BlastRadius(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class DataPlane(str, Enum):
    """Влияет ли действие на data plane (StatefulSet/PVC/DB)."""
    NO = "no"
    MAYBE = "maybe"
    YES = "yes"


class Freshness(str, Enum):
    """Свежесть сигнала: только что начался / повторяющийся / древний."""
    FRESH = "fresh"
    CHRONIC = "chronic"
    STALE = "stale"


class Confidence(str, Enum):
    """Качество атрибуции сервиса/класса — weak ниже policy gate."""
    WEAK = "weak"
    MEDIUM = "medium"
    STRONG = "strong"


class Reversibility(str, Enum):
    EASY = "easy"
    PARTIAL = "partial"
    HARD = "hard"


class Idempotency(str, Enum):
    SAFE = "safe"
    GUARDED = "guarded"
    UNSAFE = "unsafe"


# --- Mapping helpers -----------------------------------------------------

# Prefix-based namespace tiering — устроено по реальной WO topology
# (ref_wo_namespaces_layout): `<env>-shared` + `<env>-kingdom<N>` +
# `squad-N-*`, plus `monitoring`/`logging`/etc.
_NS_PROD_PREFIXES = ("prod-", "prod_", "production-")
_NS_PREPROD_PREFIXES = ("preprod-", "stage-", "staging-")
_NS_DEV_PREFIXES = ("dev-", "test-", "qa-")
_NS_SQUAD_PREFIXES = ("squad-",)
_NS_SYSTEM = frozenset({
    "kube-system",
    "kube-public",
    "kube-node-lease",
    "monitoring",
    "logging",
    "sre-ai",
    "ingress-nginx",
    "cert-manager",
    "metallb-system",
    "longhorn-system",
    "default",
})


def _classify_namespace(ns: str | None) -> NamespaceTier:
    """Map raw namespace string -> NamespaceTier.

    Unknown / empty -> SYSTEM (наиболее ограничительный — block by default).
    """
    if not ns:
        return NamespaceTier.SYSTEM
    n = ns.strip().lower()
    if n in _NS_SYSTEM:
        return NamespaceTier.SYSTEM
    if n.startswith(_NS_PROD_PREFIXES):
        return NamespaceTier.PROD
    if n.startswith(_NS_PREPROD_PREFIXES):
        return NamespaceTier.PREPROD
    if n.startswith(_NS_DEV_PREFIXES):
        return NamespaceTier.DEV
    if n.startswith(_NS_SQUAD_PREFIXES):
        return NamespaceTier.SQUAD
    # Unknown — strictly system (block default).
    return NamespaceTier.SYSTEM


_KIND_MAP = {
    "pod": ResourceKind.POD,
    "deployment": ResourceKind.DEPLOYMENT,
    "deploy": ResourceKind.DEPLOYMENT,
    "job": ResourceKind.JOB,
    "cronjob": ResourceKind.JOB,
    "statefulset": ResourceKind.STATEFULSET,
    "sts": ResourceKind.STATEFULSET,
    "pvc": ResourceKind.PVC,
    "persistentvolumeclaim": ResourceKind.PVC,
    "secret": ResourceKind.SECRET,
}


def _classify_kind(kind: str | None) -> ResourceKind:
    if not kind:
        return ResourceKind.UNKNOWN
    return _KIND_MAP.get(kind.strip().lower(), ResourceKind.UNKNOWN)


# Какие kinds считаются data-plane (имеют persistent state).
_DATA_PLANE_KINDS = frozenset({
    ResourceKind.STATEFULSET,
    ResourceKind.PVC,
    ResourceKind.SECRET,
})


@dataclass(frozen=True)
class RiskAxes:
    """Discrete 8-axis snapshot for one remediation decision.

    Frozen чтобы хешируемое значение можно было пихать в provenance, и
    случайно не мутировать после расчёта.
    """
    namespace_tier: NamespaceTier
    resource_kind: ResourceKind
    blast_radius: BlastRadius
    data_plane: DataPlane
    freshness: Freshness
    confidence: Confidence
    reversibility: Reversibility
    idempotency: Idempotency

    def to_dict(self) -> dict[str, str]:
        """Serializable форма для JSON-column / API output."""
        return {k: v.value if isinstance(v, Enum) else v
                for k, v in asdict(self).items()}


def _infer_blast_radius(
    target: Mapping[str, Any],
    classification_signals: Mapping[str, Any] | None,
) -> BlastRadius:
    """Approximate blast radius based on resource scope.

    - `none` — операция типа `kubectl get` (не наш случай в Phase A — все
      playbook-и пишут).
    - `low`  — single pod / single job в squad/dev ns.
    - `medium` — Deployment с replicas <= 3 или Job/CronJob.
    - `high`  — StatefulSet, PVC, или большой Deployment / data plane.
    """
    kind = _classify_kind(target.get("kind"))
    if kind in _DATA_PLANE_KINDS:
        return BlastRadius.HIGH
    replicas = target.get("replicas")
    if isinstance(replicas, int) and replicas >= 6:
        return BlastRadius.HIGH
    if kind == ResourceKind.POD:
        return BlastRadius.LOW
    if kind == ResourceKind.JOB:
        # Failed Job без owner — low blast (мы удаляем артефакт). С owner
        # CronJob — тоже low (CronJob воссоздаст).
        return BlastRadius.LOW
    if kind == ResourceKind.DEPLOYMENT:
        # Default — medium (rollout затрагивает все replicas).
        return BlastRadius.MEDIUM
    if (classification_signals or {}).get("affected_replicas_pct", 0) >= 50:
        return BlastRadius.HIGH
    return BlastRadius.MEDIUM


def _infer_data_plane(target: Mapping[str, Any]) -> DataPlane:
    """Data-plane уверенность.

    StatefulSet/PVC/Secret -> YES.
    Pod с owner StatefulSet -> YES.
    Deployment с label `data-plane=true` -> MAYBE.
    Иначе NO.
    """
    kind = _classify_kind(target.get("kind"))
    if kind in _DATA_PLANE_KINDS:
        return DataPlane.YES
    owner_kind = (target.get("owner_kind") or "").lower()
    if owner_kind in ("statefulset", "sts"):
        return DataPlane.YES
    labels = target.get("labels") or {}
    if isinstance(labels, dict):
        if str(labels.get("data-plane", "")).lower() == "true":
            return DataPlane.MAYBE
        if "db" in (labels.get("app", "") or "").lower():
            return DataPlane.MAYBE
    return DataPlane.NO


def _infer_freshness(signals: Mapping[str, Any] | None) -> Freshness:
    """Берём из signals: `alert_age_minutes`, `stale_class`, `chronic_score`.

    - alert_age <= 30 min -> FRESH
    - chronic_score >= 0.5 (или stale_class='suspicious_stale') -> CHRONIC
    - alert_age > 24h -> STALE (но мы стараемся не трогать stale alerts)
    """
    s = signals or {}
    stale_class = (s.get("stale_class") or "").lower()
    if stale_class in ("suspicious_stale", "stale"):
        return Freshness.STALE
    if stale_class == "chronic" or float(s.get("chronic_score") or 0) >= 0.5:
        return Freshness.CHRONIC
    age_min = s.get("alert_age_minutes")
    try:
        age = float(age_min) if age_min is not None else None
    except (TypeError, ValueError):
        age = None
    if age is None:
        return Freshness.FRESH
    if age <= 30:
        return Freshness.FRESH
    if age <= 24 * 60:
        return Freshness.CHRONIC
    return Freshness.STALE


def _infer_confidence(
    target: Mapping[str, Any],
    classification_signals: Mapping[str, Any] | None,
) -> Confidence:
    """Confidence по target + classification provenance.

    Target unknown / owner unknown -> WEAK.
    Single signal -> MEDIUM.
    Multiple signals (KG + alert label + pod_event) -> STRONG.
    """
    if not target.get("name") or not target.get("kind"):
        return Confidence.WEAK
    sources = target.get("resolved_via") or []
    if isinstance(sources, (list, tuple)) and len(sources) >= 2:
        return Confidence.STRONG
    if (classification_signals or {}).get("multi_signal"):
        return Confidence.STRONG
    return Confidence.MEDIUM


def _infer_reversibility(
    target: Mapping[str, Any],
    playbook_hint: Mapping[str, Any] | None = None,
) -> Reversibility:
    """Reversibility того, что playbook собирается сделать.

    StatefulSet / PVC / Secret delete -> HARD (data loss).
    Deployment rollout undo с known previous_revision -> EASY.
    Pod delete (owner Deployment) -> EASY (controller recreates).
    Job delete с owner CronJob -> EASY (next schedule recreates).
    Job delete без owner -> PARTIAL (one-off potentially forensic-важный).
    """
    kind = _classify_kind(target.get("kind"))
    owner_kind = (target.get("owner_kind") or "").lower()

    if kind in _DATA_PLANE_KINDS:
        return Reversibility.HARD
    if kind == ResourceKind.POD and owner_kind in (
        "deployment", "replicaset", "daemonset",
    ):
        return Reversibility.EASY
    if kind == ResourceKind.POD and owner_kind in ("statefulset", "sts"):
        return Reversibility.HARD
    if kind == ResourceKind.JOB:
        if owner_kind == "cronjob":
            return Reversibility.EASY
        # One-off Job, owner None -> partial (forensic value).
        return Reversibility.PARTIAL
    if kind == ResourceKind.DEPLOYMENT:
        # rollout undo с известной previous revision — обратимо. Если
        # playbook вынуждает delete deployment, это уже не наш случай в
        # safe registry.
        if (playbook_hint or {}).get("has_previous_revision"):
            return Reversibility.EASY
        return Reversibility.PARTIAL
    return Reversibility.PARTIAL


def _infer_idempotency(
    target: Mapping[str, Any],
    playbook_hint: Mapping[str, Any] | None = None,
) -> Idempotency:
    """Idempotency планируемой команды.

    `kubectl delete --ignore-not-found` -> SAFE.
    `kubectl rollout undo` -> GUARDED (повтор после успеха снова откатит).
    `kubectl scale` -> UNSAFE (повторное применение может изменить состояние).
    Default — GUARDED.
    """
    cmd = (playbook_hint or {}).get("command_kind") or ""
    cmd = cmd.lower()
    if cmd == "delete":
        return Idempotency.SAFE
    if cmd in ("rollout_undo", "restart"):
        return Idempotency.GUARDED
    if cmd in ("scale", "patch_resources"):
        return Idempotency.UNSAFE
    return Idempotency.GUARDED


def compute_risk_axes(
    target: Mapping[str, Any],
    classification_signals: Mapping[str, Any] | None = None,
    playbook_hint: Mapping[str, Any] | None = None,
) -> RiskAxes:
    """Construct an 8-axis snapshot for one decision.

    Все sub-функции pure — без БД, без k8s API. Это позволяет тестировать
    snapshot матрицу детерминированно.

    Args:
        target: TargetRef.to_dict() или близкий dict с keys:
            `kind`, `namespace`, `name`, `owner_kind`, `labels`, `replicas`,
            `resolved_via` (list).
        classification_signals: signals из ClassificationResult — `stale_class`,
            `alert_age_minutes`, `chronic_score`, `multi_signal`,
            `affected_replicas_pct`.
        playbook_hint: hint из playbook.plan, чтобы reversibility/idempotency
            знали что мы собираемся делать: `command_kind`
            (delete/rollout_undo/scale/restart), `has_previous_revision`.
    """
    return RiskAxes(
        namespace_tier=_classify_namespace(target.get("namespace")),
        resource_kind=_classify_kind(target.get("kind")),
        blast_radius=_infer_blast_radius(target, classification_signals),
        data_plane=_infer_data_plane(target),
        freshness=_infer_freshness(classification_signals),
        confidence=_infer_confidence(target, classification_signals),
        reversibility=_infer_reversibility(target, playbook_hint),
        idempotency=_infer_idempotency(target, playbook_hint),
    )
