"""Claude Code CLI как backend для LLM-вызовов.

Subprocess-обёртка вокруг `claude --print <prompt>` в headless-режиме —
позволяет гонять полный pipeline без Anthropic API key, опираясь на
авторизацию CLI пользователя. Используется в локальной разработке /
PoC; не предназначено для production (один CLI-spawn ≈ 1–3 секунды
overhead вверх к LLM-латенси).

Тот же паттерн, что в `external/mcp/discord-bot/src/agent_runner.py` —
там Claude Code CLI запускается с подключёнными MCP-серверами.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from typing import Optional

logger = logging.getLogger(__name__)


class ClaudeCliService:
    """Async wrapper around `claude --print`.

    Поведение совместимо с LLMService.generate_content(prompt) → str.
    """

    def __init__(
        self,
        binary: Optional[str] = None,
        timeout_seconds: float = 180.0,
        model: Optional[str] = None,
        extra_args: Optional[list[str]] = None,
    ):
        self.binary = binary or shutil.which("claude") or "claude"
        self.timeout = timeout_seconds
        self.model = model
        self.extra_args = list(extra_args or [])

    async def generate_content(self, prompt: str) -> str:
        cmd = [self.binary, "--print"]
        if self.model:
            cmd += ["--model", self.model]
        cmd += self.extra_args
        # Prompt — через stdin, чтобы не упереться в лимит argv для длинных
        # инцидентов.
        # ANTHROPIC_API_KEY вычищаем из subprocess env: при наличии этой
        # переменной Claude Code CLI переключается в "external API key"
        # режим и игнорирует CLI-авторизацию пользователя. Если в env
        # placeholder типа `__not_used__` (как в нашем e2e скрипте) — CLI
        # упадёт с "Invalid API key". Чистим явно.
        child_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env,
            )
        except FileNotFoundError as e:
            raise RuntimeError(f"claude CLI not found: {self.binary!r}") from e
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode()), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"claude CLI timed out after {self.timeout}s")
        elapsed = time.monotonic() - start
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()[:800]
            raise RuntimeError(
                f"claude CLI exit {proc.returncode} ({elapsed:.1f}s): {err}"
            )
        text = stdout.decode(errors="replace").strip()
        if not text:
            raise ValueError(f"claude CLI returned empty output ({elapsed:.1f}s)")
        logger.info("claude_cli ok in %.1fs (len=%d)", elapsed, len(text))
        return text
