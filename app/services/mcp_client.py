"""Минимальный async MCP HTTP client (streamable_http transport).

Поддерживает single-shot `tools/list` и `tools/call` для серверов из
`external/mcp` monorepo (FastMCP с `streamable_http_app`). Сервер
терпит запросы без session init для tools/* — этого достаточно для
наших серверного-к-серверному вызовов.

Auth: если передан bearer token — кладём в Authorization. Иначе — без.

Парсинг ответа: сервер может вернуть либо application/json, либо
text/event-stream (SSE) — нормализуем к python dict.
"""

from __future__ import annotations

import itertools
import json
from typing import Any, Optional

import httpx


class McpHttpClient:
    def __init__(
        self,
        url: str,
        bearer_token: Optional[str] = None,
        timeout: float = 10.0,
    ):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        self._url = url
        self._client = httpx.AsyncClient(timeout=timeout, headers=headers)
        self._id = itertools.count(1)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _rpc(self, method: str, params: Optional[dict] = None) -> dict[str, Any]:
        body = {
            "jsonrpc": "2.0",
            "id": next(self._id),
            "method": method,
            "params": params or {},
        }
        r = await self._client.post(self._url, json=body)
        r.raise_for_status()
        text = r.text
        if text.startswith("event:"):
            # SSE: первый data: блок — наш ответ
            for line in text.splitlines():
                if line.startswith("data: "):
                    return json.loads(line[6:])
            raise RuntimeError(f"empty SSE response: {text[:200]}")
        return json.loads(text)

    async def call_tool(self, name: str, arguments: Optional[dict] = None) -> Any:
        """Вызвать MCP tool. Возвращает structuredContent.result (рекомендованный
        формат FastMCP) или текстовый content при отсутствии structuredContent.
        Бросает RuntimeError на JSON-RPC error.
        """
        resp = await self._rpc(
            "tools/call", {"name": name, "arguments": arguments or {}}
        )
        if "error" in resp:
            raise RuntimeError(f"MCP tool error [{name}]: {resp['error']}")
        result = resp.get("result", {})
        if "structuredContent" in result:
            sc = result["structuredContent"]
            # FastMCP оборачивает list/dict-результат в {"result": ...}
            return sc.get("result", sc)
        # fallback: текстовый content
        content = result.get("content", [])
        if content and content[0].get("type") == "text":
            try:
                return json.loads(content[0]["text"])
            except json.JSONDecodeError:
                return content[0]["text"]
        return result
