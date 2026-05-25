"""YAML playbook loader + strict schema validation.

Schema requirements:
- `schema_version` mandatory; only `remediation.playbook/v1` accepted.
- `kind: remediation` mandatory.
- Pydantic v2 model with `model_config={"extra": "forbid"}` — typo'и
  в YAML падают на parse, не на использовании.

Без strict schema YAML rot за месяц: накопятся опечатки в `auto:`/`approve:`/
`block:` и при review никто не заметит дрейф.
"""
from __future__ import annotations

import os
from typing import Any, Iterable

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator


class PlaybookValidationError(ValueError):
    """Raised when a playbook YAML fails schema validation.

    Wraps pydantic ValidationError into single message с указанием файла.
    """


# --- Sub-models ----------------------------------------------------------

# Reusable strict base — forbid extra keys, иначе typo'и в YAML тихо
# проскакивают.
class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _NumericConstraint(_StrictModel):
    """Числовое условие в `match` секции (`{gte: 24}` / `{eq: 0}` / `{lte: 60}`)."""
    gte: float | int | None = None
    lte: float | int | None = None
    gt: float | int | None = None
    lt: float | int | None = None
    eq: float | int | None = None
    ne: float | int | None = None

    def evaluate(self, value: float | int | None) -> bool:
        """Apply все указанные констрейнты (AND) на value.

        None value -> False (отсутствие сигнала не удовлетворяет констрейнт).
        """
        if value is None:
            return False
        for attr, op in (
            ("gte", lambda v, b: v >= b),
            ("lte", lambda v, b: v <= b),
            ("gt", lambda v, b: v > b),
            ("lt", lambda v, b: v < b),
            ("eq", lambda v, b: v == b),
            ("ne", lambda v, b: v != b),
        ):
            bound = getattr(self, attr)
            if bound is not None and not op(value, bound):
                return False
        return True


class _MatchSection(_StrictModel):
    """Условие срабатывания playbook-а: classification + numeric guards.

    `classification` mandatory — без этого playbook нельзя матчить. Остальные
    поля — optional numeric guards с явным набором.
    """
    classification: str
    job_age_hours: _NumericConstraint | None = None
    active_jobs: _NumericConstraint | None = None
    failed_jobs: _NumericConstraint | None = None
    alert_age: _NumericConstraint | None = None
    recent_deploy_age: _NumericConstraint | None = None
    affected_replicas_pct: _NumericConstraint | None = None


class _AutoSection(_StrictModel):
    """`policy.auto` — должен совпасть полностью, чтобы decision был auto.

    Все поля optional, но хотя бы одно должно быть задано, иначе auto
    деградирует в «всегда auto», что небезопасно. Валидация в @field_validator.
    """
    namespace_tier: list[str] | None = None
    owner_kind: str | list[str] | None = None
    blast_radius: list[str] | None = None
    data_plane: list[str] | None = None
    logs_captured_or_ttl: bool | None = None


class _ApproveSection(_StrictModel):
    """`policy.approve` — fallback когда auto не сработал."""
    namespace_tier: list[str] | None = None
    owner_kind: str | list[str] | None = None
    blast_radius: list[str] | None = None
    data_plane: list[str] | None = None


class _BlockAnySection(_StrictModel):
    """`policy.block.any` — любое из условий = block (OR семантика).

    Block-инварианты перебивают auto/approve (см. policy.py).
    """
    namespace_tier: list[str] | None = None
    resource_kind: list[str] | None = None
    data_plane: list[str] | None = None
    reversibility: list[str] | None = None
    confidence: list[str] | None = None


class _BlockSection(_StrictModel):
    any: _BlockAnySection | None = None


class _PolicySection(_StrictModel):
    auto: _AutoSection | None = None
    approve: _ApproveSection | None = None
    block: _BlockSection | None = None


class _PlanSection(_StrictModel):
    """Команда remediation в виде списка argv (НЕ shell).

    `command` — реальная команда (НЕ выполняется в Phase A — только
    подставляется в preview как строка).
    `preview` — read-only вспомогательная команда для contextual UI
    (`kubectl get` / `rollout history`).
    """
    command: list[str]
    preview: list[str] | None = None


class _ObserveSuccessFailure(_StrictModel):
    """Optional sub-секции observe — strict, но в Phase A не используются."""
    model_config = ConfigDict(extra="allow", frozen=True)


class _ObserveSection(_StrictModel):
    """`observe` — used by Phase B observer; в Phase A только хранится."""
    timeout: str
    success: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None


# --- Root playbook -------------------------------------------------------


class Playbook(_StrictModel):
    """Top-level YAML schema. `extra=forbid` ловит typo'и.

    `schema_version` mandatory: только `remediation.playbook/v1` принимается.
    Это даёт стабильную точку для migration на v2 в будущем.
    """
    schema_version: str
    name: str
    kind: str
    description: str | None = None
    match: _MatchSection
    policy: _PolicySection
    plan: _PlanSection
    observe: _ObserveSection | None = None

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, v: str) -> str:
        if v != "remediation.playbook/v1":
            raise ValueError(
                f"schema_version must be 'remediation.playbook/v1', got '{v}'"
            )
        return v

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, v: str) -> str:
        if v != "remediation":
            raise ValueError(f"kind must be 'remediation', got '{v}'")
        return v


# --- Loader --------------------------------------------------------------


def load_playbook(path: str) -> Playbook:
    """Load + validate single YAML file. Raise PlaybookValidationError при provblem."""
    if not os.path.exists(path):
        raise PlaybookValidationError(f"playbook not found: {path}")
    with open(path, encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise PlaybookValidationError(
                f"YAML parse error in {path}: {e}",
            ) from e
    if not isinstance(data, dict):
        raise PlaybookValidationError(
            f"playbook root must be a mapping (got {type(data).__name__}): {path}",
        )
    try:
        return Playbook.model_validate(data)
    except ValidationError as e:
        raise PlaybookValidationError(
            f"playbook schema validation failed for {path}:\n{e}",
        ) from e


def _iter_yaml_files(directory: str) -> Iterable[str]:
    for entry in sorted(os.listdir(directory)):
        if entry.startswith(".") or entry.startswith("_"):
            continue
        if not (entry.endswith(".yaml") or entry.endswith(".yml")):
            continue
        yield os.path.join(directory, entry)


def load_registry(directory: str | None = None) -> dict[str, Playbook]:
    """Load all *.yaml from registry directory, return dict {name -> Playbook}.

    По умолчанию — `app/remediation/registry/`. Дубликаты по имени → raise.
    """
    if directory is None:
        directory = os.path.join(os.path.dirname(__file__), "registry")
    if not os.path.isdir(directory):
        raise PlaybookValidationError(f"registry dir not found: {directory}")
    result: dict[str, Playbook] = {}
    for path in _iter_yaml_files(directory):
        pb = load_playbook(path)
        if pb.name in result:
            raise PlaybookValidationError(
                f"duplicate playbook name '{pb.name}' in {path}",
            )
        result[pb.name] = pb
    return result
