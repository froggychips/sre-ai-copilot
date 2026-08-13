"""Replay-бэкенд LLM для golden-eval: записанные ответы вместо вызовов модели.

Зачем: eval должен гоняться на каждом PR — то есть без сети, без ключа, без
денег и детерминированно. Записанные ответы дают ровно это: цепочка
hypothesis → critique → fix → gate прогоняется целиком, но «мозг» отвечает
одинаково от прогона к прогону.

Что этим меряется (и что НЕ меряется) — важно не перепутать:
  * МЕРЯЕТСЯ детерминированный обвес вокруг модели: парсинг гипотез,
    anchor-грounding в FactCritic, отбор выжившего, сборка ExecutionIntent,
    политика executor-гейта, suppression. Именно здесь живут регрессии, и
    именно они ломались в прошлом (см. combat runs 1–3 в docs/RUNBOOK.md).
  * НЕ МЕРЯЕТСЯ качество самой модели на новом промпте: ответ записан от
    старого. Для этого есть live-режим (scripts/eval_golden.py --mode live),
    он ходит в Anthropic и переписывает записи.

Точка перехвата — ModelRouter.route_and_call_full: через неё ходят ВСЕ
агенты (BaseAgent.ask), поэтому prompt_guard, обработка truncation и разбор
ответа остаются настоящими — подменяется только сам вызов модели.

Ключ записи — (роль агента, контекст запроса), оба в виде sha256:
  * роль стабильна к переписыванию инструкции (Task), т.е. правку промпта
    replay переживает;
  * контекст различает несколько вызовов одной роли (FactCritic идёт по
    гипотезам) и в replay-прогоне детерминирован — предыдущие ответы тоже
    из записи.
Если точного ключа нет, берётся n-й по счёту ответ этой же роли; если и его
нет — MissingRecording, и кейс честно падает с указанием, что перезаписать.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = ["Recordings", "MissingRecording", "install_replay", "install_recorder"]

# BaseAgent.ask собирает промпт как:
#   Role: {role}\nTask: {instruction}\n<user_context>\n{ctx}\n</user_context>
_ROLE_RE = re.compile(r"^Role:\s*(.*?)\nTask:", re.DOTALL | re.MULTILINE)
_CTX_RE = re.compile(r"<user_context>\n(.*?)\n</user_context>", re.DOTALL)


class MissingRecording(RuntimeError):
    """В записи нет ответа для этого вызова — прогон нельзя считать валидным."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _role_of(prompt: str) -> str:
    m = _ROLE_RE.search(prompt)
    return _sha((m.group(1) if m else prompt[:200]).strip())


def _ctx_of(prompt: str) -> str:
    m = _CTX_RE.search(prompt)
    return _sha((m.group(1) if m else prompt).strip())


class Recordings:
    """Записанные ответы одного golden-кейса.

    Формат файла (tests/golden/recordings/<case_id>.json):
        {"calls": [{"role": "<sha>", "ctx": "<sha>", "task_type": "hypothesis",
                    "text": "...", "model": "...", "recorded_at": "..."}]}
    Порядок в списке — порядок вызовов при записи; он же используется как
    запасной ключ.
    """

    def __init__(self, calls: Optional[List[Dict[str, Any]]] = None) -> None:
        self.calls: List[Dict[str, Any]] = list(calls or [])
        self._used_by_role: Dict[str, int] = {}
        self.misses: List[str] = []

    @classmethod
    def load(cls, path: Path) -> "Recordings":
        if not path.exists():
            return cls([])
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data.get("calls") or [])

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"calls": self.calls}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def add(self, prompt: str, task_type: str, result: Dict[str, Any]) -> None:
        self.calls.append({
            "role": _role_of(prompt),
            "ctx": _ctx_of(prompt),
            "task_type": task_type,
            "text": result.get("text", ""),
        })

    def lookup(self, prompt: str) -> Dict[str, Any]:
        role, ctx = _role_of(prompt), _ctx_of(prompt)
        for call in self.calls:
            if call.get("role") == role and call.get("ctx") == ctx:
                return self._as_result(call)
        # Контекст разъехался (например, поменялся формат FactStore-контекста) —
        # берём n-й ответ этой роли. Прогон остаётся валидным, но такой матч
        # означает, что записи пора обновить.
        same_role = [c for c in self.calls if c.get("role") == role]
        idx = self._used_by_role.get(role, 0)
        self._used_by_role[role] = idx + 1
        if idx < len(same_role):
            self.misses.append(f"ctx-miss role={role[:8]} #{idx}")
            return self._as_result(same_role[idx])
        raise MissingRecording(
            f"нет записи для role={role[:8]} (вызов #{idx}); "
            f"перезаписать: scripts/eval_golden.py --mode record"
        )

    @staticmethod
    def _as_result(call: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "text": call.get("text", ""),
            "input_tokens": 0,
            "output_tokens": 0,
            "model": "replay",
            "backend": "replay",
            "truncated": False,
        }


def install_replay(monkeypatch_like, recordings: Recordings) -> None:
    """Подменить ModelRouter на чтение из записей.

    monkeypatch_like — объект с методом setattr(obj, name, value): pytest-овый
    monkeypatch либо простая обёртка из scripts/eval_golden.py.
    """
    from app.llm.router import ModelRouter

    async def _full(task_type: str, prompt: str):
        return recordings.lookup(prompt)

    async def _text(task_type: str, prompt: str) -> str:
        return recordings.lookup(prompt).get("text", "")

    monkeypatch_like.setattr(ModelRouter, "route_and_call_full", staticmethod(_full))
    monkeypatch_like.setattr(ModelRouter, "route_and_call", staticmethod(_text))


def install_recorder(monkeypatch_like, recordings: Recordings) -> None:
    """Пропускать вызовы в настоящую модель и складывать ответы в записи."""
    from app.llm.router import ModelRouter

    original = ModelRouter.route_and_call_full

    async def _full(task_type: str, prompt: str):
        result = await original(task_type, prompt)
        if not isinstance(result, dict):  # старый text-only контракт
            result = {"text": str(result)}
        recordings.add(prompt, task_type, result)
        return result

    monkeypatch_like.setattr(ModelRouter, "route_and_call_full", staticmethod(_full))
