"""YAML playbook loader + strict schema validation.

Покрытие:
- schema_version mandatory + must equal `remediation.playbook/v1`
- kind mandatory + must equal `remediation`
- `extra=forbid` ловит typo в keys
- registry loader находит sample playbook
- _NumericConstraint.evaluate работает для gte/lte/eq/None
"""
from __future__ import annotations

import pytest

from app.remediation.playbook import (Playbook, PlaybookValidationError,
                                      _NumericConstraint, load_playbook,
                                      load_registry)


def _write_fixture(tmp_path, name: str, content: str) -> str:
    """Write a YAML fixture to pytest tmp_path. Returns full path string."""
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_load_real_sample_playbook() -> None:
    """Real registry/cleanup_stale_failed_job.yaml загружается без ошибок."""
    registry = load_registry()
    assert "cleanup_stale_failed_job" in registry
    pb = registry["cleanup_stale_failed_job"]
    assert isinstance(pb, Playbook)
    assert pb.schema_version == "remediation.playbook/v1"
    assert pb.kind == "remediation"
    assert pb.match.classification == "stale_failed_job"
    assert pb.policy.auto is not None
    assert pb.policy.auto.namespace_tier == ["dev", "squad"]
    assert pb.policy.block is not None
    assert pb.policy.block.any is not None
    assert pb.policy.block.any.namespace_tier == ["prod", "preprod", "system"]


def test_missing_schema_version_rejected(tmp_path) -> None:
    path = _write_fixture(tmp_path,"missing_schema.yaml", """
name: foo
kind: remediation
match:
  classification: stale_failed_job
policy: {}
plan:
  command: ["kubectl", "get", "pods"]
""")
    with pytest.raises(PlaybookValidationError):
        load_playbook(path)


def test_wrong_schema_version_rejected(tmp_path) -> None:
    path = _write_fixture(tmp_path,"wrong_schema.yaml", """
schema_version: remediation.playbook/v999
name: foo
kind: remediation
match:
  classification: stale_failed_job
policy: {}
plan:
  command: ["kubectl", "get", "pods"]
""")
    with pytest.raises(PlaybookValidationError) as excinfo:
        load_playbook(path)
    assert "schema_version" in str(excinfo.value)


def test_wrong_kind_rejected(tmp_path) -> None:
    path = _write_fixture(tmp_path,"wrong_kind.yaml", """
schema_version: remediation.playbook/v1
name: foo
kind: not_remediation
match:
  classification: stale_failed_job
policy: {}
plan:
  command: ["kubectl"]
""")
    with pytest.raises(PlaybookValidationError):
        load_playbook(path)


def test_extra_field_in_root_forbidden(tmp_path) -> None:
    """`extra=forbid` — typo на root уровне падает."""
    path = _write_fixture(tmp_path,"extra_root.yaml", """
schema_version: remediation.playbook/v1
name: foo
kind: remediation
unkown_typo_field: yes
match:
  classification: stale_failed_job
policy: {}
plan:
  command: ["kubectl"]
""")
    with pytest.raises(PlaybookValidationError):
        load_playbook(path)


def test_extra_field_in_policy_auto_forbidden(tmp_path) -> None:
    """Typo в `policy.auto` — должны падать на parse."""
    path = _write_fixture(tmp_path,"extra_auto.yaml", """
schema_version: remediation.playbook/v1
name: foo
kind: remediation
match:
  classification: stale_failed_job
policy:
  auto:
    namespace_tier: ["dev"]
    namspace_tier_typo: ["dev"]
plan:
  command: ["kubectl"]
""")
    with pytest.raises(PlaybookValidationError):
        load_playbook(path)


def test_match_classification_required(tmp_path) -> None:
    """`match.classification` mandatory."""
    path = _write_fixture(tmp_path,"no_class.yaml", """
schema_version: remediation.playbook/v1
name: foo
kind: remediation
match:
  job_age_hours: {gte: 24}
policy: {}
plan:
  command: ["kubectl"]
""")
    with pytest.raises(PlaybookValidationError):
        load_playbook(path)


def test_plan_command_required(tmp_path) -> None:
    path = _write_fixture(tmp_path,"no_command.yaml", """
schema_version: remediation.playbook/v1
name: foo
kind: remediation
match:
  classification: stale_failed_job
policy: {}
plan: {}
""")
    with pytest.raises(PlaybookValidationError):
        load_playbook(path)


def test_numeric_constraint_gte_lte_eq() -> None:
    c = _NumericConstraint(gte=24)
    assert c.evaluate(24) is True
    assert c.evaluate(48) is True
    assert c.evaluate(23) is False
    assert c.evaluate(None) is False

    c2 = _NumericConstraint(eq=0)
    assert c2.evaluate(0) is True
    assert c2.evaluate(1) is False

    c3 = _NumericConstraint(lte=60, gte=10)
    assert c3.evaluate(30) is True
    assert c3.evaluate(5) is False
    assert c3.evaluate(120) is False


def test_load_playbook_yaml_parse_error(tmp_path) -> None:
    path = _write_fixture(tmp_path,"bad.yaml", ":\n:\n:\nnot: yaml: at all\n")
    with pytest.raises(PlaybookValidationError):
        load_playbook(path)


def test_load_playbook_missing_file() -> None:
    with pytest.raises(PlaybookValidationError):
        load_playbook("/tmp/nonexistent-playbook-xyz.yaml")
