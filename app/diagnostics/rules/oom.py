"""OOMKilled detector.

Ищет в контексте признаки OOM-событий. Приоритеты (по убыванию точности):
  1. k8s_pod_state — terminated.reason/exit_code из k8s API (наиболее точно)
  2. Текстовый regex — только если k8s_pod_state не опровергает OOM

ВАЖНО: если k8s_pod_state содержит данные о target-поде с non-OOM exit code
(≠ 0, ≠ 137), текстовый fallback подавляется. Иначе "OOMKilled" из событий
соседних подов namespace даёт ложный positive.

Конкретные паттерны взяты из реальных текстов k8s events:
    "OOMKilled"
    "out of memory"
    "Container ... was OOM killed"
    "exit code 137"          # SIGKILL, чаще всего OOM, но не всегда
    "memory cgroup out of memory"
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from app.diagnostics.facts import Fact, FactKind
from app.diagnostics.rules.base import Rule, same_workload

# Сильные индикаторы — high-confidence.
_HARD_PATTERN = re.compile(
    r"(oom\s?killed|out of memory|memory cgroup out of memory|killed.*?oom)",
    re.IGNORECASE,
)
# Слабый: exit 137 может быть от ручного kill -9, не только OOM.
_SOFT_PATTERN = re.compile(r"exit\s*code\s*137|\bsigkill\b", re.IGNORECASE)


class OOMKilledRule(Rule):
    name = "OOMKilledRule"

    def evaluate(self, ctx: Dict[str, Any]) -> List[Fact]:
        pod = ctx.get("pod") or ctx.get("service")
        pod_state = ctx.get("k8s_pod_state") or {}

        # ── 1. Structured: k8s terminated state ──────────────────────────
        # Сканируем target-под первым, потом остальные (вдруг пересоздан).
        structured, target_exit = self._check_pod_state(pod_state, pod)
        if structured is not None:
            return [structured]

        # ── 2. Блокировка text-fallback при наличии non-OOM данных ───────
        # Если k8s API вернул данные о target-поде с exit code ≠ 137,
        # любые текстовые совпадения "OOMKilled" — шум из соседних подов.
        if target_exit is not None and target_exit not in (0, 137):
            return [
                Fact(
                    kind=FactKind.OOM_KILLED,
                    observed=False,
                    confidence=0.90,
                    subject=pod,
                    evidence={
                        "source": "k8s_terminated_state",
                        "exit_code": target_exit,
                        "note": "non-OOM exit code; text matches suppressed",
                    },
                    source_rule=self.name,
                )
            ]

        # ── 3. Text regex fallback ────────────────────────────────────────
        text = self.text_haystack(ctx)
        hard = self.count_matches(text, _HARD_PATTERN)
        soft = self.count_matches(text, _SOFT_PATTERN)

        if hard >= 1:
            return [
                Fact(
                    kind=FactKind.OOM_KILLED,
                    observed=True,
                    confidence=0.95,
                    subject=pod,
                    evidence={"hard_matches": hard, "soft_matches": soft},
                    source_rule=self.name,
                )
            ]
        if soft >= 1:
            # exit 137 без явного OOMKilled — слабый сигнал.
            return [
                Fact(
                    kind=FactKind.OOM_KILLED,
                    observed=True,
                    confidence=0.4,
                    subject=pod,
                    evidence={"soft_matches": soft, "note": "exit 137 only"},
                    source_rule=self.name,
                )
            ]

        # ── 4. ✗ — OOM не обнаружен ──────────────────────────────────────
        return [
            Fact(
                kind=FactKind.OOM_KILLED,
                observed=False,
                confidence=0.9,
                subject=pod,
                evidence={},
                source_rule=self.name,
            )
        ]

    def _check_pod_state(
        self, pod_state: Dict[str, Any], pod: Optional[str]
    ) -> Tuple[Optional[Fact], Optional[int]]:
        """Возвращает (Fact | None, target_exit_code | None).

        Fact — если нашли OOM в structured данных.
        target_exit_code — exit code target-пода (для блокировки text-fallback),
            None если target-под отсутствует в pod_state или exit_code не задан.

        Скоуп: pod_state содержит ВСЕ unhealthy-поды namespace-а. Учитываем
        только target-под и поды того же workload-а (пересозданный под с
        другим rs-hash) — OOM НЕсвязанного сервиса в том же namespace не
        должен приписываться этому инциденту (раньше первый попавшийся сосед
        давал false anchor с conf 0.98). Если target неизвестен вовсе —
        скоупить не по чему, сканируем всё (старое поведение).
        """
        candidates = []
        if pod and pod in pod_state:
            candidates.append((pod, pod_state[pod]))
        for pod_name, info in pod_state.items():
            if pod_name == pod:
                continue
            if pod and not same_workload(pod_name, pod):
                continue
            candidates.append((pod_name, info))

        target_exit: Optional[int] = None
        if pod and pod in pod_state:
            target_exit = pod_state[pod].get("exit_code")

        for pod_name, info in candidates:
            reason = (info.get("reason") or "").strip()
            exit_code = info.get("exit_code")

            if reason == "OOMKilled":
                return (
                    Fact(
                        kind=FactKind.OOM_KILLED,
                        observed=True,
                        confidence=0.98,
                        subject=pod_name,
                        evidence={
                            "source": "k8s_terminated_state",
                            "exit_code": exit_code,
                            "container": info.get("container", ""),
                        },
                        source_rule=self.name,
                    ),
                    target_exit,
                )

            if exit_code == 137:
                # exit 137 без OOMKilled — может быть kill -9 или OOM.
                return (
                    Fact(
                        kind=FactKind.OOM_KILLED,
                        observed=True,
                        confidence=0.55,
                        subject=pod_name,
                        evidence={
                            "source": "k8s_terminated_state",
                            "exit_code": 137,
                            "note": "exit 137 without explicit OOMKilled reason",
                            "container": info.get("container", ""),
                        },
                        source_rule=self.name,
                    ),
                    target_exit,
                )

        return None, target_exit
