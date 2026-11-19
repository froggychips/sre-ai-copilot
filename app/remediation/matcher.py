"""Детерминированный отбор v2-playbook-ов под инцидент и рендер их плана.

    alertname / classification + FactStore
        -> match_playbooks        (match + preconditions, без LLM)
        -> кандидаты для FixAgent (за флагом REMEDIATION_PLAYBOOK_BINDING_ENABLED)
        -> executor_gate сверяет intent с планом выбранного playbook-а

Отбор намеренно не зависит от модели: какие действия вообще допустимы для
этого инцидента, решают факты и YAML, а LLM выбирает только среди них. Так
prompt-injection в логах пода может в худшем случае выбрать другой
допустимый playbook, но не придумать действие, которого в плане нет.

v1-playbook-и (preview-only) сюда не попадают: их матчит
`preview.build_decision_preview` по classification + numeric-сигналам.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Mapping

from app.core.execution_dsl import (ActionType, DSLTranslator, ExecutionIntent,
                                    action_spec)
from app.diagnostics.facts import FactStore, Verdict
from app.remediation.playbook import Playbook, load_registry, template_name

__all__ = [
    "ANY_TARGET_KIND",
    "REASON_TARGET_KIND_MISMATCH",
    "REASON_TARGET_KIND_UNKNOWN",
    "PlanRenderError",
    "check_preconditions",
    "classify_alert",
    "default_registry",
    "match_playbooks",
    "render_plan",
    "target_kind_rejection",
]

#: Значение `target_kind` по умолчанию: вид цели не проверять. Так зовут
#: matcher те, у кого цели ещё нет (pipeline отбирает кандидатов для FixAgent
#: до выбора цели, golden-реплей) — их поведение не меняется.
ANY_TARGET_KIND = "*"

REASON_TARGET_KIND_MISMATCH = "target_kind_mismatch"
REASON_TARGET_KIND_UNKNOWN = "target_kind_unknown"


class PlanRenderError(ValueError):
    """План нельзя отрендерить: шаблон без значения или intent невалиден."""


@lru_cache(maxsize=1)
def default_registry() -> Mapping[str, Playbook]:
    """Реестр `app/remediation/registry/`, загруженный один раз на процесс.

    YAML меняется только вместе с образом, перечитывать его на каждый алерт
    незачем. Ошибка схемы здесь всплывает исключением — вызывающий обязан
    трактовать её fail-closed (gate → BLOCK, FixAgent — без кандидатов).
    """
    return load_registry()


def check_preconditions(
    playbook: Playbook, facts: FactStore | None,
) -> tuple[bool, list[dict[str, Any]]]:
    """Все preconditions playbook-а против FactStore (AND).

    Семантика вердиктов (fail-closed):
      * `found`  — есть хотя бы один FOUND-факт этого kind;
      * `absent` — есть ABSENT-факт, и НЕТ ни FOUND, ни UNKNOWN того же kind.
        Один упавший источник (UNKNOWN) при другом «не нашёл» (ABSENT) — это
        пробел, а не доказательство отсутствия;
      * факта нет вовсе — правило не отработало, условие не выполнено;
      * `evidence_not_in` (только с found) — ни один FOUND-факт не несёт
        запрещённого значения, и у каждого ключ evidence есть.

    Возвращает (ok, checks) — checks пригодны для audit/embed.
    """
    checks: list[dict[str, Any]] = []
    for pre in playbook.preconditions or ():
        kinds = facts.by_kind(pre.fact) if facts is not None else []
        verdicts = {f.verdict for f in kinds}
        excluded: list[dict[str, Any]] = []
        if pre.verdict == Verdict.FOUND.value:
            found = [f for f in kinds if f.verdict == Verdict.FOUND.value]
            ok = bool(found)
            for key, banned in (pre.evidence_not_in or {}).items():
                for f in found:
                    if key not in f.evidence or f.evidence[key] in banned:
                        excluded.append({"key": key, "value": f.evidence.get(key, "missing")})
            ok = ok and not excluded
        else:
            ok = verdicts == {Verdict.ABSENT.value}
        check: dict[str, Any] = {
            "fact": pre.fact,
            "expected": pre.verdict,
            "actual": sorted(verdicts) or ["missing"],
            "ok": ok,
        }
        if excluded:
            check["excluded_by_evidence"] = excluded
        checks.append(check)
        if not ok:
            return False, checks
    return True, checks


def classify_alert(labels: Mapping[str, Any] | None) -> str | None:
    """Classification инцидента по лейблам алерта — для `match.classification`.

    Тот же классификатор, что у preview (`classifier.classify`), но без KG и
    без enrichment-сигналов: стадия FixAgent не должна ходить в БД ради
    отбора кандидатов. Классы, которым нужны сигналы (возраст Job-а, число
    упавших), здесь не определятся — UNKNOWN возвращается как None, и
    playbook с `match.classification` такой инцидент просто не выберет
    (fail-closed). Ошибка классификатора — тоже None.
    """
    from app.remediation.classifier import Classification, classify
    from app.remediation.target_resolver import resolve_target
    try:
        target = resolve_target({"labels": dict(labels or {})}, kg_session=None)
        result = classify(target.to_dict(), {})
    except Exception:
        return None
    if result.classification == Classification.UNKNOWN:
        return None
    return result.classification.value


def _match_section_ok(
    playbook: Playbook, alertname: str | None, classification: str | None,
) -> bool:
    match = playbook.match
    if match.alertnames and (alertname or "") not in match.alertnames:
        return False
    if match.classification is not None and match.classification != classification:
        return False
    return True


def target_kind_rejection(playbook: Playbook, target_kind: str | None) -> str | None:
    """Почему цель этого вида playbook-у не подходит; None — подходит.

    `match.target_kinds` не задан — вид не проверяется. Задан: вид вне
    списка — `target_kind_mismatch`; вид неизвестен (None) —
    `target_kind_unknown`. Неизвестное не считается подходящим: playbook,
    умеющий только Deployment, на «не знаем что» потратил бы пробу kubectl и
    в лучшем случае получил бы NotFound (fail-closed).
    """
    kinds = playbook.match.target_kinds
    if not kinds or target_kind == ANY_TARGET_KIND:
        return None
    if not target_kind:
        return REASON_TARGET_KIND_UNKNOWN
    if target_kind not in kinds:
        return REASON_TARGET_KIND_MISMATCH
    return None


def match_playbooks(
    registry: Mapping[str, Playbook] | None = None,
    *,
    alertname: str | None = None,
    classification: str | None = None,
    facts: FactStore | None = None,
    target_kind: str | None = ANY_TARGET_KIND,
    rejected: list[tuple[str, str]] | None = None,
) -> list[Playbook]:
    """Исполнимые (v2) playbook-и, чей match и preconditions выполнены.

    Порядок — по имени (как в реестре), чтобы кандидаты в промпте и в
    аудите не зависели от порядка обхода файлов.

    `target_kind` — вид рабочей нагрузки цели (`Deployment`, ...), None —
    «не удалось определить»; по умолчанию не проверяется. Сверка с
    `match.target_kinds` идёт ДО preconditions: она дешевле и не зависит от
    фактов. В `rejected` (если передан) дописываются `(playbook, причина)`
    отсечённых по виду — для счётчика и лога вызывающего.
    """
    if registry is None:
        registry = default_registry()
    result: list[Playbook] = []
    for name in sorted(registry):
        pb = registry[name]
        if not pb.executable:
            continue
        if not _match_section_ok(pb, alertname, classification):
            continue
        reason = target_kind_rejection(pb, target_kind)
        if reason is not None:
            if rejected is not None:
                rejected.append((pb.name, reason))
            continue
        ok, _ = check_preconditions(pb, facts)
        if ok:
            result.append(pb)
    return result


def _render_param(value: int | str, context: Mapping[str, Any]) -> Any:
    key = template_name(value)
    if key is not None:
        if key not in context or context[key] is None:
            raise PlanRenderError(f"template '{{{key}}}' has no value")
        return context[key]
    return value


def render_plan(
    playbook: Playbook,
    *,
    namespace: str,
    resource_name: str,
    context: Mapping[str, Any] | None = None,
) -> list[tuple[ExecutionIntent, list[str]]]:
    """Шаги плана → [(ExecutionIntent, argv)] через execution_dsl.

    Каждый шаг проходит полную валидацию ExecutionIntent (charset имён,
    FORBIDDEN_NAMESPACES, диапазон replicas) и argv собирает
    `DSLTranslator.to_argv` — тот же путь, что у executor-а. Ничего не
    выполняется. Шаблон без значения → PlanRenderError, а не подстановка
    пустоты.
    """
    if not playbook.executable:
        raise PlanRenderError(f"playbook '{playbook.name}' is not v2 (preview-only)")
    ctx = dict(context or {})
    rendered: list[tuple[ExecutionIntent, list[str]]] = []
    for step in playbook.plan.steps or ():
        action = ActionType(step.action)
        spec = action_spec(action)
        params = {k: _render_param(v, ctx) for k, v in step.params.items()}
        missing = [p for p in spec.required_params if p not in params]
        if missing:
            raise PlanRenderError(
                f"step '{step.action}' is missing required params {missing}"
            )
        try:
            intent = ExecutionIntent(
                action=action,
                resource_type=(
                    step.resource_type or spec.requires_resource_type or "deployment"
                ),
                resource_name=resource_name,
                namespace=namespace,
                params=params,
                playbook=playbook.name,
            )
        except Exception as e:
            raise PlanRenderError(
                f"step '{step.action}' produced invalid intent: {e}"
            ) from e
        rendered.append((intent, DSLTranslator.to_argv(intent)))
    return rendered
