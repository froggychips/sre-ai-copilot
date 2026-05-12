"""ProcessCrashRule — детектор краша процесса по сигналу.

Покрывает случаи, когда контейнер умер не от OOM (exit 137/SIGKILL),
а от бага в коде: segfault, abort, illegal instruction и т.д.

Источники (в порядке убывания приоритета):
  1. k8s_pod_state — terminated.exit_code из k8s API (наиболее точно)
  2. k8s_events    — Event reason "OOMKilling" исключён, остальные Error-события
  3. Regex по logs_summary / k8s_summary — exit code в тексте логов

Linux exit code = 128 + signal number:
    139 = SIGSEGV  (segfault — null deref, heap/stack corruption)
    134 = SIGABRT  (abort() / assert() / C++ exception unhandled)
    132 = SIGILL   (illegal CPU instruction — JIT-баг, bad native interop)
    135 = SIGBUS   (bus error — unaligned access, mmap issue)
    136 = SIGFPE   (floating point exception — div by zero)
    138 = SIGUSR1  (намеренный crash-dump триггер в некоторых рантаймах)

OOMKilledRule владеет exit 137 — здесь не трогаем.
SIGTERM (143) = graceful shutdown — не crash, игнорируем.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from app.diagnostics.facts import Fact, FactKind
from app.diagnostics.rules.base import Rule

# exit_code → (signal_name, confidence)
_CRASH_EXIT_CODES: Dict[int, Tuple[str, float]] = {
    139: ("SIGSEGV",  0.97),
    134: ("SIGABRT",  0.95),
    132: ("SIGILL",   0.93),
    135: ("SIGBUS",   0.93),
    136: ("SIGFPE",   0.90),
    138: ("SIGUSR1",  0.70),  # может быть намеренный crash-dump
}

# Catch-all: любой ненулевой exit, не OOM (137) и не graceful (0, 143)
_GENERIC_CRASH_CODES = frozenset(range(1, 256)) - {0, 137, 143}

_EXIT_CODE_PATTERN = re.compile(
    r"exit\s*code[:\s]+(\d+)|exited\s+with\s+(?:code\s+)?(\d+)", re.IGNORECASE
)


def _signal_label(exit_code: int) -> str:
    signals = {
        139: "SIGSEGV", 134: "SIGABRT", 132: "SIGILL",
        135: "SIGBUS",  136: "SIGFPE",  138: "SIGUSR1",
        137: "SIGKILL", 143: "SIGTERM", 130: "SIGINT",
    }
    return signals.get(exit_code, f"signal={exit_code - 128}" if exit_code > 128 else "")


class ProcessCrashRule(Rule):
    name = "ProcessCrashRule"

    def evaluate(self, ctx: Dict[str, Any]) -> List[Fact]:
        pod = ctx.get("pod") or ctx.get("service")

        # ── 1. Structured: k8s terminated state ──────────────────────────
        pod_state = ctx.get("k8s_pod_state") or {}
        structured_fact = self._check_pod_state(pod_state, pod)
        if structured_fact is not None:
            if structured_fact.observed and ctx.get("core_dump_node"):
                structured_fact.evidence["core_dump_node"] = ctx["core_dump_node"]
            return [structured_fact]

        # ── 2. Regex fallback на текст логов ──────────────────────────────
        text = self.text_haystack(ctx)
        for m in _EXIT_CODE_PATTERN.finditer(text):
            code_str = m.group(1) or m.group(2)
            try:
                code = int(code_str)
            except ValueError:
                continue
            if code in (0, 137, 143):
                continue  # OOM и graceful — не наш случай
            signal, conf = _CRASH_EXIT_CODES.get(code, ("", 0.45))
            if code not in _CRASH_EXIT_CODES and code not in _GENERIC_CRASH_CODES:
                continue
            return [
                Fact(
                    kind=FactKind.PROCESS_CRASH,
                    observed=True,
                    confidence=conf,
                    subject=pod,
                    evidence={
                        "source": "log_regex",
                        "exit_code": code,
                        "signal": signal or _signal_label(code),
                    },
                    source_rule=self.name,
                )
            ]

        # ── 3. ✗ — явное «краша не нашли» ────────────────────────────────
        return [
            Fact(
                kind=FactKind.PROCESS_CRASH,
                observed=False,
                confidence=0.70,
                subject=pod,
                source_rule=self.name,
            )
        ]

    def _core_dump_evidence(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        node = ctx.get("core_dump_node")
        return {"core_dump_node": node} if node else {}

    def _check_pod_state(
        self, pod_state: Dict[str, Any], pod: Optional[str]
    ) -> Optional[Fact]:
        """Проверяем terminated state для target-пода, затем для всех остальных."""
        candidates = []
        if pod and pod in pod_state:
            candidates.append((pod, pod_state[pod]))
        # Проверяем все поды namespace — вдруг target уже пересоздан
        for pod_name, info in pod_state.items():
            if pod_name != pod:
                candidates.append((pod_name, info))

        for pod_name, info in candidates:
            exit_code = info.get("exit_code")
            reason = (info.get("reason") or "").strip()

            if exit_code is None:
                continue
            if exit_code in (0, 137, 143):
                continue  # OOM/graceful — чужие

            signal, conf = _CRASH_EXIT_CODES.get(exit_code, ("", 0.0))

            # reason="Error" + known crash exit code → высокая уверенность
            if exit_code in _CRASH_EXIT_CODES:
                return Fact(
                    kind=FactKind.PROCESS_CRASH,
                    observed=True,
                    confidence=conf,
                    subject=pod_name,
                    evidence={
                        "source": "k8s_terminated_state",
                        "exit_code": exit_code,
                        "signal": signal,
                        "reason": reason,
                        "container": info.get("container", ""),
                    },
                    source_rule=self.name,
                )

            # reason="Error" + неизвестный exit code → слабый сигнал
            if reason == "Error" and exit_code != 0:
                return Fact(
                    kind=FactKind.PROCESS_CRASH,
                    observed=True,
                    confidence=0.55,
                    subject=pod_name,
                    evidence={
                        "source": "k8s_terminated_state",
                        "exit_code": exit_code,
                        "signal": _signal_label(exit_code),
                        "reason": reason,
                        "container": info.get("container", ""),
                    },
                    source_rule=self.name,
                )

        return None
