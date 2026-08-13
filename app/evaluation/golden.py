"""Golden-eval: прогон зафиксированных инцидентов через RCA-цепочку.

Проект автоматически проверяет всё, кроме того, ради чего он существует:
ruff, mypy, bandit, pip-audit, coverage-gate, KG contract drift — есть, а
качество разбора инцидента меряется шестью ручными «боевыми прогонами» в
README и 👍/👎 в Discord. Этот модуль закрывает дыру: набор зафиксированных
кейсов + метрики, которым CI не даёт просесть.

Что прогоняется (ядро, без БД/k8s/VM — их роль играют данные кейса):

    incident + context ─► DiagnosticEngine ─► FactStore      [детерминизм]
                                  │
                                  ▼
                        MultiHypothesisAgent                 [LLM]
                                  │
                                  ▼
                          FactCriticAgent ─► best_candidate  [LLM + отбор]
                                  │
                                  ▼
                            FixAgent ─► ExecutionIntent      [LLM]
                                  │
                                  ▼
                        evaluate_intent_gate ─► BLOCK/APPROVE [детерминизм]

Кейс объявляет ожидания на любом из этих уровней, поэтому набор ловит и
regressions «модель стала хуже», и regressions «обвес перестал работать».
Часть кейсов намеренно не содержит LLM-ожиданий вообще (`llm: false`) — это
инварианты безопасности, которые обязаны выполняться при любом ответе модели.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from app.agents.fact_critic import FactCriticAgent, best_candidate
from app.agents.fix import FixAgent
from app.agents.multi_hypothesis import MultiHypothesisAgent
from app.core.execution_dsl import ExecutionIntent
from app.diagnostics import default_engine as diag_engine
from app.diagnostics.incident_ctx import build_diagnostics_ctx
from app.models.incident import Incident
from app.remediation.executor_gate import evaluate_intent_gate

GOLDEN_DIR = Path(__file__).resolve().parents[2] / "tests" / "golden"
CASES_DIR = GOLDEN_DIR / "cases"
RECORDINGS_DIR = GOLDEN_DIR / "recordings"
BASELINE_PATH = GOLDEN_DIR / "baseline.json"


# --- Кейс -----------------------------------------------------------------


@dataclass
class GoldenCase:
    """Один зафиксированный инцидент с ожиданиями.

    Поля YAML:
      id, title, source: real|synthetic, llm: bool (нужна ли LLM-цепочка)
      incident:  payload алерта (как приходит из AlertManager)
      context:   доп. поля ctx для DiagnosticEngine (k8s_pod_state,
                 logs_summary, recent_deployments, ...) — то, что в проде
                 собирает stage_diagnose
      intent:    готовый ExecutionIntent для кейсов-инвариантов гейта
      expect:    facts / cause_contains / cause_not_contains / resolution /
                 intent_action / gate
    """

    id: str
    title: str
    path: Path
    source: str = "synthetic"
    llm: bool = True
    incident: Dict[str, Any] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)
    intent: Optional[Dict[str, Any]] = None
    expect: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "GoldenCase":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(
            id=raw.get("id") or path.stem,
            title=raw.get("title", ""),
            path=path,
            source=raw.get("source", "synthetic"),
            llm=bool(raw.get("llm", True)),
            incident=raw.get("incident") or {},
            context=raw.get("context") or {},
            intent=raw.get("intent"),
            expect=raw.get("expect") or {},
        )

    @property
    def recording_path(self) -> Path:
        return RECORDINGS_DIR / f"{self.id}.json"


def load_cases(only: Optional[List[str]] = None) -> List[GoldenCase]:
    cases = [GoldenCase.load(p) for p in sorted(CASES_DIR.glob("*.yaml"))]
    if only:
        wanted = set(only)
        cases = [c for c in cases if c.id in wanted]
    return cases


# --- Результат ------------------------------------------------------------


@dataclass
class CaseResult:
    case_id: str
    checks: Dict[str, bool] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    cause: Optional[str] = None
    error: Optional[str] = None
    # Кейс не прогонялся: в replay-режиме для него нет записанных ответов.
    # Именно пропуск, а не провал: иначе baseline зафиксировал бы отсутствие
    # записи как «норму продукта», и метрика начала бы врать в удобную сторону.
    skipped: bool = False
    skip_reason: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(self.checks.values())

    @property
    def failed_checks(self) -> List[str]:
        return [k for k, ok in self.checks.items() if not ok]


# --- Прогон ---------------------------------------------------------------


def build_ctx(case: GoldenCase) -> Dict[str, Any]:
    """ctx для DiagnosticEngine — через тот же адаптер, что и прод."""
    incident = Incident(**case.incident)
    # analyzer_summary намеренно пустой: вывод AnalyzerAgent не входит в
    # text_haystack (circular fact contamination), поэтому на факты он не
    # влияет и в фикстуре не нужен.
    ctx = build_diagnostics_ctx(incident, analyzer_summary="", kg_session=None)
    ctx.update(case.context)
    return ctx


def check_facts(store, expected: Dict[str, bool], result: CaseResult) -> None:
    observed = store.observed_kinds()
    for kind, should_be in expected.items():
        actual = kind in observed
        result.checks[f"fact:{kind}"] = actual is bool(should_be)
        if actual is not bool(should_be):
            result.notes.append(
                f"факт {kind}: ожидали observed={should_be}, получили {actual}"
            )


def check_gate(intent: Optional[ExecutionIntent], expected: str, result: CaseResult) -> None:
    """Детерминированный executor-гейт: block/approve/auto либо none (нет intent-а)."""
    if intent is None:
        result.checks["gate"] = expected == "none"
        if expected != "none":
            result.notes.append(f"гейт: ожидали {expected}, но ExecutionIntent не собран")
        return
    decision = evaluate_intent_gate(intent)
    actual = decision.mode.value
    result.checks["gate"] = actual == expected
    if actual != expected:
        result.notes.append(
            f"гейт: ожидали {expected}, получили {actual} "
            f"({[r.get('axis') for r in decision.reasons]})"
        )


async def run_case(case: GoldenCase) -> CaseResult:
    """Прогнать один кейс. LLM берётся из того, что установлено вызывающим."""
    result = CaseResult(case_id=case.id)
    expect = case.expect

    # 1. Кейс-инвариант гейта: готовый intent, никакой LLM.
    if case.intent is not None:
        try:
            intent = ExecutionIntent(**case.intent)
        except Exception as e:
            # Часть инвариантов срабатывает РАНЬШЕ гейта — на валидации самого
            # intent-а (FORBIDDEN_NAMESPACES, replicas=0). Для таких кейсов
            # ожидание — именно отказ конструктора.
            result.checks["intent_rejected"] = bool(expect.get("intent_rejected"))
            if not expect.get("intent_rejected"):
                result.error = f"ExecutionIntent не собрался: {type(e).__name__}: {e}"
            else:
                result.notes.append(f"intent отвергнут на валидации: {type(e).__name__}")
            return result
        if expect.get("intent_rejected"):
            result.checks["intent_rejected"] = False
            result.notes.append("ожидали отказ валидации ExecutionIntent, но он собрался")
            return result
        check_gate(intent, expect.get("gate", "block"), result)
        return result

    # 2. Факты — детерминированная часть, есть у всех кейсов.
    try:
        store = diag_engine.run(build_ctx(case))
    except Exception as e:
        result.error = f"DiagnosticEngine упал: {type(e).__name__}: {e}"
        return result
    if expect.get("facts"):
        check_facts(store, expect["facts"], result)

    if not case.llm:
        return result

    # 3. LLM-цепочка: гипотезы → критика → отбор → fix → гейт.
    try:
        summary = case.incident.get("summary") or case.title
        hypotheses = await MultiHypothesisAgent().generate(
            incident_summary=summary, facts=store
        )
        critiqued = await FactCriticAgent().critique_all(hypotheses, store)
    except Exception as e:
        result.error = f"LLM-цепочка упала: {type(e).__name__}: {e}"
        return result

    best = best_candidate(critiqued)
    result.cause = best.cause if best else None

    if "resolution" in expect:
        actual = "resolved" if best else "unresolved"
        result.checks["resolution"] = actual == expect["resolution"]
        if actual != expect["resolution"]:
            result.notes.append(f"resolution: ожидали {expect['resolution']}, получили {actual}")

    if expect.get("cause_contains"):
        # Совпадение по ключевым словам, а не по точному тексту: причина —
        # свободная проза, требовать дословности бессмысленно. Достаточно
        # одного попадания из списка — список это синонимы одной причины.
        low = (result.cause or "").lower()
        hit = [w for w in expect["cause_contains"] if w.lower() in low]
        result.checks["cause_contains"] = bool(hit)
        if not hit:
            result.notes.append(
                f"причина не содержит ни одного из {expect['cause_contains']}: "
                f"{(result.cause or '—')[:120]}"
            )

    if expect.get("cause_not_contains"):
        # Анти-регрессия: причина, которая уже была ложной (OOM на exit≠137).
        low = (result.cause or "").lower()
        bad = [w for w in expect["cause_not_contains"] if w.lower() in low]
        result.checks["cause_not_contains"] = not bad
        if bad:
            result.notes.append(f"причина содержит запрещённое {bad}: {(result.cause or '')[:120]}")

    if "intent_action" in expect or "gate" in expect:
        try:
            _, intent = await FixAgent().suggest(
                result.cause or "Manual triage required", is_recurrence=False
            )
        except Exception as e:
            result.error = f"FixAgent упал: {type(e).__name__}: {e}"
            return result
        if "intent_action" in expect:
            actual = intent.action.value if intent is not None else "none"
            result.checks["intent_action"] = actual == expect["intent_action"]
            if actual != expect["intent_action"]:
                result.notes.append(
                    f"intent.action: ожидали {expect['intent_action']}, получили {actual}"
                )
        if "gate" in expect:
            check_gate(intent, expect["gate"], result)

    return result


def run_case_sync(case: GoldenCase) -> CaseResult:
    return asyncio.run(run_case(case))


# --- Метрики --------------------------------------------------------------


def summarize(results: List[CaseResult]) -> Dict[str, Any]:
    """Сводка прогона. Округление до 3 знаков — чтобы baseline не дрожал.

    Пропущенные кейсы (нет записей) в метрики не входят вообще: иначе
    незаписанный кейс тянул бы pass_rate вниз, а зафиксированный на нём
    baseline объявлял бы это нормой.
    """
    skipped = [r for r in results if r.skipped]
    results = [r for r in results if not r.skipped]
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    checks = [c for r in results for c in r.checks.items()]
    checks_passed = sum(1 for _, ok in checks if ok)

    by_check: Dict[str, Dict[str, int]] = {}
    for r in results:
        for name, ok in r.checks.items():
            group = name.split(":")[0]
            slot = by_check.setdefault(group, {"passed": 0, "total": 0})
            slot["total"] += 1
            slot["passed"] += int(ok)

    return {
        "cases_total": total,
        "cases_passed": passed,
        "cases_skipped": len(skipped),
        "case_pass_rate": round(passed / total, 3) if total else 0.0,
        "checks_total": len(checks),
        "checks_passed": checks_passed,
        "check_pass_rate": round(checks_passed / len(checks), 3) if checks else 0.0,
        "by_check": {
            k: round(v["passed"] / v["total"], 3) for k, v in sorted(by_check.items())
        },
        "errors": [r.case_id for r in results if r.error],
    }
