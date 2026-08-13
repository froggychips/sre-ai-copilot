#!/usr/bin/env python3
"""Прогон golden-набора: единственная метрика, описывающая сам продукт.

Режимы:
  --mode replay  (по умолчанию)  записанные LLM-ответы, без сети и ключа.
                 Это то, что гоняется на каждом PR: детерминированно, быстро,
                 бесплатно. Меряет обвес вокруг модели, не саму модель.
  --mode live    реальные вызовы Anthropic. Меряет качество разбора на текущих
                 промптах. Стоит денег, поэтому по расписанию и вручную.
  --mode record  как live, но ещё и перезаписывает tests/golden/recordings/ —
                 после осознанной смены промпта.

Выход: таблица по кейсам + сводка. `--check-baseline` сверяет метрики с
tests/golden/baseline.json и возвращает ненулевой код, если стало хуже;
`--update-baseline` записывает текущие метрики как новый эталон.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.evaluation.golden import (BASELINE_PATH, GoldenCase,  # noqa: E402
                                   CaseResult, load_cases, run_case, summarize)
from app.evaluation.llm_replay import (Recordings, install_recorder,  # noqa: E402
                                       install_replay)


class _Patcher:
    """Минимальный аналог pytest-овского monkeypatch: setattr + откат."""

    def __init__(self) -> None:
        self._undo: list = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
        self._undo.clear()


async def _run_one(case: GoldenCase, mode: str) -> CaseResult:
    patcher = _Patcher()
    recordings = Recordings.load(case.recording_path)
    if mode == "replay" and case.llm and not recordings.calls:
        # Записей нет — кейс не прогоняется и в метрики не попадает. Считать
        # это провалом значило бы зафиксировать в baseline отсутствие записи
        # как норму; молча проходить — врать, что кейс проверен.
        return CaseResult(
            case_id=case.id,
            skipped=True,
            skip_reason="нет записанных ответов (--mode record)",
        )
    try:
        if mode == "replay":
            install_replay(patcher, recordings)
        elif mode == "record":
            recordings = Recordings([])
            install_recorder(patcher, recordings)
        result = await run_case(case)
        if mode == "record" and case.llm and recordings.calls:
            recordings.save(case.recording_path)
        if mode == "replay" and recordings.misses:
            result.notes.append(
                f"записи разъехались с контекстом ({len(recordings.misses)} шт) — "
                "стоит перезаписать: --mode record"
            )
        return result
    finally:
        patcher.undo()


async def _main_async(args) -> int:
    cases = load_cases(args.case)
    if not cases:
        print("кейсов не найдено", file=sys.stderr)
        return 2

    if args.mode in ("live", "record"):
        # claude_cli ходит в локальный `claude --print` и ключа не требует —
        # им же удобно перезаписывать записи на машине разработчика.
        backend = os.environ.get("LLM_BACKEND", "anthropic")
        if backend == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
            print(
                "режим требует ANTHROPIC_API_KEY (или LLM_BACKEND=claude_cli)",
                file=sys.stderr,
            )
            return 2

    results = []
    for case in cases:
        result = await _run_one(case, args.mode)
        results.append(result)
        if result.skipped:
            print(f"⏭  {case.id:42} пропущен: {result.skip_reason}")
            continue
        mark = "✅" if result.passed else "❌"
        detail = ""
        if result.error:
            detail = f" ошибка: {result.error}"
        elif result.failed_checks:
            detail = f" провалено: {', '.join(result.failed_checks)}"
        print(f"{mark} {case.id:42} {len(result.checks)} проверок{detail}")
        for note in result.notes:
            print(f"     · {note}")

    summary = summarize(results)
    print("\n— сводка —")
    skipped = summary.get("cases_skipped", 0)
    print(f"кейсы:    {summary['cases_passed']}/{summary['cases_total']} "
          f"({summary['case_pass_rate']:.0%})"
          + (f", пропущено {skipped}" if skipped else ""))
    print(f"проверки: {summary['checks_passed']}/{summary['checks_total']} "
          f"({summary['check_pass_rate']:.0%})")
    for group, rate in summary["by_check"].items():
        print(f"  {group:18} {rate:.0%}")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    if args.update_baseline:
        BASELINE_PATH.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\nbaseline обновлён: {BASELINE_PATH.relative_to(REPO_ROOT)}")
        return 0

    if args.check_baseline:
        return _check_baseline(summary)

    return 0 if summary["cases_passed"] == summary["cases_total"] else 1



def _check_baseline(summary) -> int:
    """Метрики не должны просесть относительно эталона.

    Сверяем и агрегат, и разрез по группам проверок: без разреза «плюс два
    новых кейса на факты» замаскировали бы «минус один на гейт».
    """
    if not BASELINE_PATH.exists():
        print("\nbaseline отсутствует — зафиксировать: --update-baseline", file=sys.stderr)
        return 2
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    problems = []
    if summary["case_pass_rate"] < baseline.get("case_pass_rate", 0):
        problems.append(
            f"case_pass_rate {summary['case_pass_rate']:.3f} < "
            f"{baseline['case_pass_rate']:.3f}"
        )
    for group, rate in baseline.get("by_check", {}).items():
        now = summary["by_check"].get(group)
        if now is None:
            problems.append(f"группа проверок `{group}` исчезла из набора")
        elif now < rate:
            problems.append(f"{group}: {now:.3f} < {rate:.3f}")
    if problems:
        print("\nРЕГРЕСС относительно baseline:", file=sys.stderr)
        for p in problems:
            print(f"  · {p}", file=sys.stderr)
        return 1
    print("\nbaseline: без регресса")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=("replay", "live", "record"), default="replay")
    ap.add_argument("--case", action="append", help="id кейса (можно несколько раз)")
    ap.add_argument("--check-baseline", action="store_true")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--json-out", help="куда записать сводку в JSON")
    return asyncio.run(_main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
