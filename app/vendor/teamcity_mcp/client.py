"""Vendored TeamCity REST client.

Source: ~/projects/teamcity-mcp/src/teamcity_mcp/client.py (94 lines).
Skip-sync если в upstream происходит крупное API-изменение.
"""
from __future__ import annotations

import os
from typing import Any

import httpx


class TCError(Exception):
    def __init__(self, status: int, endpoint: str, body: str):
        self.status = status
        self.endpoint = endpoint
        self.body = body
        hint = ""
        if status == 401:
            hint = "TC_TOKEN missing or expired."
        elif status == 403:
            if "VIEW_BUILD_CONFIGURATION_SETTINGS" in body:
                hint = "Token lacks VIEW_BUILD_CONFIGURATION_SETTINGS on the parent project."
            elif "View audit log" in body:
                hint = "Token lacks 'View audit log' global permission."
            else:
                hint = "Token lacks required permission for this endpoint."
        elif status == 404:
            hint = "Resource not found (or hidden from this token)."
        super().__init__(f"[{status}] {endpoint} — {hint} :: {body[:300]}")


def _locator(parts: dict[str, Any]) -> str:
    return ",".join(f"{k}:{v}" for k, v in parts.items() if v is not None and v != "")


class TeamCityClient:
    def __init__(self, url: str | None = None, token: str | None = None, timeout: float = 30.0):
        self.url = (url or os.environ.get("TC_URL") or "").rstrip("/")
        self.token = token or os.environ.get("TC_TOKEN") or ""
        if not self.url or not self.token:
            raise RuntimeError("TC_URL and TC_TOKEN env vars must be set")
        self._http = httpx.Client(
            base_url=self.url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        r = self._http.get(path, params=params)
        if r.status_code >= 400:
            raise TCError(r.status_code, path, r.text)
        return r.json()

    def get_text(self, path: str, params: dict[str, Any] | None = None, max_bytes: int | None = None) -> str:
        with self._http.stream("GET", path, params=params) as r:
            if r.status_code >= 400:
                raise TCError(r.status_code, path, r.read().decode("utf-8", errors="replace"))
            chunks: list[bytes] = []
            total = 0
            for chunk in r.iter_bytes():
                if max_bytes and total + len(chunk) > max_bytes:
                    chunks.append(chunk[: max_bytes - total])
                    break
                chunks.append(chunk)
                total += len(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")

    def post_json(self, path: str, body: Any, params: dict[str, Any] | None = None) -> Any:
        r = self._http.post(
            path,
            params=params,
            json=body,
            headers={"Content-Type": "application/json"},
        )
        if r.status_code >= 400:
            raise TCError(r.status_code, path, r.text)
        if not r.content:
            return None
        ctype = r.headers.get("content-type", "")
        if "application/json" in ctype:
            return r.json()
        return r.text

    def delete(self, path: str, params: dict[str, Any] | None = None) -> None:
        r = self._http.delete(path, params=params)
        if r.status_code >= 400:
            raise TCError(r.status_code, path, r.text)


def build_locator(**parts: Any) -> str:
    return _locator(parts)
