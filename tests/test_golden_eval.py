"""Golden-набор в обычном прогоне pytest (replay-режим).

Здесь набор виден как обычные тесты — падение показывает конкретный кейс и
конкретную проверку, а не одну строку «eval упал». Полная сводка с метриками
и сверкой с baseline — отдельным шагом CI (scripts/eval_golden.py).

LLM-кейсы без записанных ответов пропускаются со skip-ом: пропуск виден в
выводе, а красный CI из-за неснятой записи только приучал бы его игнорировать.
"""
import asyncio
import json

import pytest

from app.evaluation.golden import BASELINE_PATH, load_cases, run_case, summarize
from app.evaluation.llm_replay import Recordings, install_replay

CASES = load_cases()


def test_golden_set_is_not_empty():
    """Набор не должен молча исчезнуть — иначе «всё зелено» перестанет что-то значить."""
    assert len(CASES) >= 20, f"ожидали ≥20 кейсов, найдено {len(CASES)}"


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_golden_case(case, monkeypatch):
    recordings = Recordings.load(case.recording_path)
    if case.llm and not recordings.calls:
        pytest.skip(
            f"нет записанных ответов для {case.id}: "
            "снять через scripts/eval_golden.py --mode record"
        )
    install_replay(monkeypatch, recordings)
    result = asyncio.run(run_case(case))

    assert result.error is None, f"{case.id}: {result.error}"
    assert result.passed, (
        f"{case.id} — провалено {result.failed_checks}\n"
        + "\n".join(f"  · {n}" for n in result.notes)
    )


def test_deterministic_cases_need_no_llm():
    """Инварианты гейта и фактов обязаны выполняться без единого вызова модели.

    Если сюда просочится кейс, которому нужна LLM, набор перестанет быть
    дешёвым и его начнут отключать в CI.
    """
    for case in CASES:
        if case.llm:
            continue
        result = asyncio.run(run_case(case))  # без install_replay вообще
        assert result.error is None, f"{case.id}: {result.error}"
        assert result.passed, f"{case.id} — провалено {result.failed_checks}"


def test_baseline_matches_deterministic_reality():
    """baseline существует и не обещает больше, чем набор даёт сейчас.

    Сверяем только детерминированные группы: LLM-зависимые метрики в
    replay-режиме зависят от наличия записей и проверяются отдельным шагом CI.
    """
    assert BASELINE_PATH.exists(), "baseline.json не зафиксирован"
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))

    results = []
    for case in CASES:
        if case.llm:
            continue
        results.append(asyncio.run(run_case(case)))
    summary = summarize(results)

    for group in ("fact", "gate", "intent_rejected"):
        expected = baseline.get("by_check", {}).get(group)
        if expected is None:
            continue
        actual = summary["by_check"].get(group, 0.0)
        assert actual >= expected, (
            f"группа {group} просела: {actual} < {expected} (baseline)"
        )


def _load_eval_script():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "eval_golden.py"
    spec = importlib.util.spec_from_file_location("eval_golden_script", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("update_baseline", [False, True])
def test_all_cases_skipped_is_not_success(monkeypatch, tmp_path, update_baseline):
    """«0 из 0 прошло» — не зелёный итог и не новый baseline.

    До фикса прогон, где все кейсы пропущены (нет записей / нет ключа),
    возвращал 0: cases_passed == cases_total == 0.
    """
    from types import SimpleNamespace

    from app.evaluation.golden import CaseResult

    mod = _load_eval_script()
    baseline = tmp_path / "baseline.json"
    monkeypatch.setattr(mod, "BASELINE_PATH", baseline)
    monkeypatch.setattr(mod, "load_cases", lambda ids=None: CASES[:2])

    async def _skip(case, mode):
        return CaseResult(case_id=case.id, skipped=True, skip_reason="test")

    monkeypatch.setattr(mod, "_run_one", _skip)
    args = SimpleNamespace(
        case=None, mode="replay", json_out=None,
        update_baseline=update_baseline, check_baseline=False,
    )
    assert asyncio.run(mod._main_async(args)) == 1
    assert not baseline.exists()
