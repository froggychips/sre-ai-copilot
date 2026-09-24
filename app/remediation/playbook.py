"""YAML playbook loader + strict schema validation.

Schema requirements:
- `schema_version` mandatory; `remediation.playbook/v1` или `/v2`.
- `kind: remediation` mandatory.
- Pydantic v2 model with `model_config={"extra": "forbid"}` — typo'и
  в YAML падают на parse, не на использовании.

Без strict schema YAML rot за месяц: накопятся опечатки в `auto:`/`approve:`/
`block:` и при review никто не заметит дрейф.

Две версии схемы (24.09.2026):

- **v1** — preview-only (Phase A). `plan.command` — голый argv, который
  только рендерится строкой в UI; `match` понимает classification и
  numeric-поля stale-job. Так описан `cleanup_stale_failed_job`: удаления
  Job нет в `ACTION_SPECS`, поэтому перевести его на v2 нельзя, не заводя
  новое мутирующее действие, — и это правильно.
- **v2** — исполнимый контракт. `plan.steps` ссылаются на `ActionType` из
  `app/core/execution_dsl.ACTION_SPECS`; argv собирает только
  `DSLTranslator.to_argv`, в YAML команды нет вовсе. Плюс `preconditions`
  по вердиктам фактов из FactStore и `verify` — имена проверок из
  `verify_checks.VERIFY_CHECKS`. Всё это валидируется на загрузке: playbook
  с несуществующим действием, фактом или проверкой не загрузится.
"""
from __future__ import annotations

import os
from typing import Any, Iterable, Literal

import yaml
from pydantic import (BaseModel, ConfigDict, ValidationError, field_validator,
                      model_validator)

from app.core.execution_dsl import ActionType
from app.diagnostics.facts import FactKind
from app.remediation.verify_checks import VERIFY_CHECKS

SCHEMA_V1 = "remediation.playbook/v1"
SCHEMA_V2 = "remediation.playbook/v2"
_SCHEMA_VERSIONS = (SCHEMA_V1, SCHEMA_V2)


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

    v1: `classification` обязателен — без него playbook нельзя матчить.
    v2: хотя бы одно из `classification` / `alertnames` (проверка в
    `Playbook._check_version_contract`). Остальные поля — optional numeric
    guards с явным набором.
    """
    classification: str | None = None
    # v2: точные имена алертов AlertManager. Без glob-ов: `*CrashLoop*`
    # незаметно подхватил бы новый алерт, про который автор playbook-а не думал.
    alertnames: list[str] | None = None
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


_ACTION_VALUES = frozenset(a.value for a in ActionType)


class _PlanStep(_StrictModel):
    """Шаг v2-плана: ссылка на действие из реестра execution_dsl.

    `params` — значения или шаблоны `{name}` (целиком строка), которые
    подставляются при рендере (`matcher.render_plan`). Команду шаг не несёт
    намеренно: argv собирает только `DSLTranslator.to_argv`, иначе YAML снова
    стал бы вторым, непроверяемым источником kubectl-команд.
    """
    action: str
    params: dict[str, int | str] = {}
    # По умолчанию — `requires_resource_type` из ActionSpec, иначе deployment.
    resource_type: str | None = None

    @field_validator("action")
    @classmethod
    def _check_action(cls, v: str) -> str:
        # Проверяем по ActionType, а не по ACTION_SPECS напрямую: полнота
        # реестра спецификаций гарантируется на импорте execution_dsl.
        if v not in _ACTION_VALUES:
            raise ValueError(
                f"unknown action '{v}': нет в ActionType/ACTION_SPECS "
                f"(известные: {sorted(_ACTION_VALUES)})"
            )
        return v


class _PlanSection(_StrictModel):
    """План remediation.

    v1: `command` — argv (НЕ shell), только рендерится в preview строкой;
    `preview` — read-only вспомогательная команда для contextual UI.
    v2: `steps` — действия из ACTION_SPECS; `command`/`preview` запрещены.
    """
    command: list[str] | None = None
    preview: list[str] | None = None
    steps: list[_PlanStep] | None = None


class _Precondition(_StrictModel):
    """Требование к вердикту факта из FactStore (v2).

    `verdict: unknown` в YAML не допускается: «требую, чтобы не удалось
    проверить» — бессмыслица. А UNKNOWN у факта никогда не удовлетворяет ни
    found, ни absent (fail-closed, см. `matcher.check_preconditions`).
    """
    fact: str
    verdict: Literal["found", "absent"]
    # Только с `verdict: found`: каждый FOUND-факт обязан нести ключ evidence,
    # и его значение не должно входить в список. Нужен, когда kind слишком
    # широкий: process_crash FOUND и у SIGSEGV (139), и у транзиентного exit 1.
    # Ключа в evidence нет → условие не выполнено (fail-closed).
    evidence_not_in: dict[str, list[int | str]] | None = None

    @model_validator(mode="after")
    def _check_evidence_filter(self) -> "_Precondition":
        if self.evidence_not_in is not None:
            if self.verdict != "found":
                raise ValueError("evidence_not_in applies only to verdict: found")
            if not self.evidence_not_in:
                raise ValueError("evidence_not_in must not be empty")
        return self

    @field_validator("fact")
    @classmethod
    def _check_fact(cls, v: str) -> str:
        if v not in FactKind.ALL:
            raise ValueError(
                f"unknown fact kind '{v}' (известные: {sorted(FactKind.ALL)})"
            )
        return v


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
    preconditions: list[_Precondition] | None = None
    policy: _PolicySection
    plan: _PlanSection
    verify: list[str] | None = None
    observe: _ObserveSection | None = None

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, v: str) -> str:
        if v not in _SCHEMA_VERSIONS:
            raise ValueError(
                f"schema_version must be one of {list(_SCHEMA_VERSIONS)}, got '{v}'"
            )
        return v

    @field_validator("verify")
    @classmethod
    def _check_verify(cls, v: list[str] | None) -> list[str] | None:
        for name in v or ():
            if name not in VERIFY_CHECKS:
                raise ValueError(
                    f"unknown verify check '{name}' "
                    f"(известные: {sorted(VERIFY_CHECKS)})"
                )
        return v

    @model_validator(mode="after")
    def _check_version_contract(self) -> "Playbook":
        """Поля v1 и v2 не смешиваются.

        Молча игнорировать «чужое» поле нельзя: `steps` в v1 выглядели бы
        исполнимыми, а не исполнялись бы; `command` в v2 — вторым источником
        argv в обход DSL.
        """
        if self.schema_version == SCHEMA_V1:
            if self.match.classification is None:
                raise ValueError("v1: match.classification is required")
            if self.plan.command is None:
                raise ValueError("v1: plan.command is required")
            v2_only = [
                name for name, present in (
                    ("match.alertnames", self.match.alertnames is not None),
                    ("plan.steps", self.plan.steps is not None),
                    ("preconditions", self.preconditions is not None),
                    ("verify", self.verify is not None),
                ) if present
            ]
            if v2_only:
                raise ValueError(f"v1 does not support: {v2_only} (use v2)")
        else:
            if not self.plan.steps:
                raise ValueError("v2: plan.steps is required and non-empty")
            if self.plan.command is not None or self.plan.preview is not None:
                raise ValueError(
                    "v2: plan.command/plan.preview are forbidden — argv "
                    "собирается только через execution_dsl"
                )
            if self.match.classification is None and not self.match.alertnames:
                raise ValueError(
                    "v2: match needs classification or non-empty alertnames"
                )
        return self

    @property
    def executable(self) -> bool:
        """v2 — исполнимый контракт; v1 — только preview."""
        return self.schema_version == SCHEMA_V2

    def step_actions(self) -> frozenset[str]:
        """Имена действий плана (пусто у v1)."""
        return frozenset(step.action for step in self.plan.steps or ())

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
