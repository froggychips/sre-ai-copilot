"""TargetRef resolution из alert labels / facts / KG.

Покрытие:
- pure alert labels (Deployment, StatefulSet, Job, Pod);
- facts override побеждает alert labels;
- unknown target когда нет ns/kind/name;
- KG enrichment добавляет owner_kind/team_owner;
- resolved_via provenance дедуплицируется.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.remediation.target_resolver import resolve_target


def test_alert_deployment_label() -> None:
    alert = {
        "labels": {
            "namespace": "squad-3-shared",
            "deployment": "town-service",
            "team_owner": "squad-gd",
        },
    }
    ref = resolve_target(alert)
    assert ref.kind == "Deployment"
    assert ref.namespace == "squad-3-shared"
    assert ref.name == "town-service"
    assert ref.unknown is False
    assert "alert_label" in ref.resolved_via
    assert ref.labels["team_owner"] == "squad-gd"


def test_alert_statefulset_priority_over_deployment() -> None:
    # StatefulSet идёт раньше Deployment в порядке matching — это намеренно
    # (StatefulSet более конкретный сигнал).
    alert = {
        "labels": {
            "namespace": "prod-shared",
            "deployment": "fallback",
            "statefulset": "town-db",
        },
    }
    ref = resolve_target(alert)
    assert ref.kind == "StatefulSet"
    assert ref.name == "town-db"


def test_alert_job_label() -> None:
    alert = {
        "labels": {
            "namespace": "squad-3-shared",
            "job_name": "migrate-202605240130",
            "owner_kind": "CronJob",
            "owner_name": "migrate",
        },
    }
    ref = resolve_target(alert)
    assert ref.kind == "Job"
    assert ref.name == "migrate-202605240130"
    assert ref.owner_kind == "CronJob"
    assert ref.owner_name == "migrate"


def test_alert_pod_label() -> None:
    alert = {
        "labels": {
            "namespace": "dev-3",
            "pod": "town-service-abc-xyz",
        },
    }
    ref = resolve_target(alert)
    assert ref.kind == "Pod"
    assert ref.name == "town-service-abc-xyz"


def test_unknown_target_when_no_labels() -> None:
    ref = resolve_target({"labels": {}})
    assert ref.unknown is True
    assert ref.kind is None and ref.name is None


def test_unknown_target_when_only_namespace() -> None:
    ref = resolve_target({"labels": {"namespace": "squad-3"}})
    assert ref.unknown is True
    assert ref.namespace == "squad-3"


def test_facts_override_alert_labels() -> None:
    """`facts.target.*` имеет приоритет над alert labels."""
    alert = {
        "labels": {
            "namespace": "dev-1",
            "deployment": "from-alert",
        },
    }
    facts = {"target": {"name": "from-facts", "replicas": 3}}
    ref = resolve_target(alert, facts=facts)
    # name перебито из facts; namespace остался из alert.
    assert ref.name == "from-facts"
    assert ref.namespace == "dev-1"
    assert ref.replicas == 3
    assert "explicit_facts" in ref.resolved_via


def test_facts_override_kind() -> None:
    alert = {"labels": {"namespace": "dev-1", "pod": "p1"}}
    facts = {"target": {"kind": "Deployment", "name": "d1"}}
    ref = resolve_target(alert, facts=facts)
    assert ref.kind == "Deployment"
    assert ref.name == "d1"


def test_kg_enrichment_adds_owner_for_job() -> None:
    """KG K8sJob row provides owner_kind=CronJob."""
    alert = {
        "labels": {
            "namespace": "squad-3-shared",
            "job_name": "backup-202605240130",
        },
    }
    row = SimpleNamespace(
        metadata_json={"owner_kind": "CronJob", "owner_name": "backup"},
        active_count=0,
        failed_count=2,
    )

    class _FakeQuery:
        def __init__(self, ret: object | None) -> None:
            self._ret = ret
        def filter(self, *_a: object, **_k: object) -> "_FakeQuery":
            return self
        def first(self) -> object | None:
            return self._ret
        def order_by(self, *_a: object, **_k: object) -> "_FakeQuery":
            return self

    class _FakeSession:
        def query(self, _cls: object) -> _FakeQuery:
            from app.knowledge_graph.schema import K8sJob, PodEvent, Service
            if _cls is K8sJob:
                return _FakeQuery(row)
            if _cls is PodEvent:
                return _FakeQuery(None)
            if _cls is Service:
                return _FakeQuery(None)
            return _FakeQuery(None)
        def get(self, _cls: object, _id: int) -> object | None:
            return None

    ref = resolve_target(alert, kg_session=_FakeSession())
    assert ref.owner_kind == "CronJob"
    assert ref.owner_name == "backup"
    assert "kg_k8s_job" in ref.resolved_via
    assert ref.labels.get("_kg_failed_jobs") == "2"
    assert ref.labels.get("_kg_active_jobs") == "0"


def test_resolved_via_dedup() -> None:
    """Дублирующиеся источники в resolved_via убираются с сохранением порядка."""
    alert = {
        "labels": {"namespace": "dev-1", "deployment": "svc"},
    }
    facts = {"target": {"labels": {"app": "svc"}}}
    ref = resolve_target(alert, facts=facts)
    # alert_label + explicit_facts добавлены каждый по одному разу.
    assert ref.resolved_via.count("alert_label") == 1
    assert ref.resolved_via.count("explicit_facts") == 1


def test_to_dict_roundtrip() -> None:
    alert = {"labels": {"namespace": "dev-1", "deployment": "svc"}}
    ref = resolve_target(alert)
    d = ref.to_dict()
    assert d["kind"] == "Deployment"
    assert d["unknown"] is False
    # labels всегда dict, resolved_via всегда list.
    assert isinstance(d["labels"], dict)
    assert isinstance(d["resolved_via"], list)
