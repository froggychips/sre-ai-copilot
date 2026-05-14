"""TeamCity → recent-deploys context для incident-а.

Два транспорта (в порядке приоритета):
  1. Прямой TC REST API через TeamCityClient из локального пакета teamcity_mcp.
     Активен когда TC_URL + TC_TOKEN заданы в конфиге.
     Sync-клиент запускается в thread pool (run_in_executor).
  2. MCP HTTP server (McpHttpClient) — если задан TEAMCITY_MCP_URL.
     Нужен задеплоенный mcp-teamcity-server (пока не поднят).

Публичный интерфейс `incident_teamcity_context()` не изменился.
Graceful degrade (None) если ни один транспорт не доступен.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
import structlog

from app.config import settings
from app.services.mcp_client import McpHttpClient

# Пакет teamcity_mcp — устанавливается отдельно (pip install teamcity-mcp).
# В dev можно передать путь через TC_MCP_SRC env var.
_tc_mcp_src = os.environ.get("TC_MCP_SRC", "")
if _tc_mcp_src and _tc_mcp_src not in sys.path:
    sys.path.insert(0, _tc_mcp_src)
try:
    from teamcity_mcp.client import TeamCityClient as _TCClient
    _TC_CLIENT_AVAILABLE = True
except ImportError:
    _TC_CLIENT_AVAILABLE = False

logger = structlog.get_logger()

# k8s-namespace → логическое имя ветки в TC. Покрывает основные prod-сценарии WO.
# squad-N (любое число) намеренно вернёт None: на squad деплоят с разных feature-веток
# вручную, корреляция «текущий push в default → деплой» не работает.
_BRANCH_RULES = [
    (re.compile(r"^prod(-|$)"), "prod"),
    (re.compile(r"^preprod(-|$)"), "preprod"),
    (re.compile(r"^preupdate(-|$)"), "preupdate"),
    (re.compile(r"^squad-gd(-|$)"), "preprod"),
]


def branch_for_namespace(namespace: Optional[str]) -> Optional[str]:
    if not namespace:
        return None
    for pat, branch in _BRANCH_RULES:
        if pat.match(namespace):
            return branch
    return None


def _trim_comment(c: str, n: int = 140) -> str:
    c = (c or "").strip().splitlines()[0] if c else ""
    return c[: n - 1] + "…" if len(c) > n else c


def _parse_tc_date(s: str) -> Optional[datetime]:
    """TC отдаёт `yyyyMMdd'T'HHmmss±HHmm` без двоеточий."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%S%z")
    except ValueError:
        return None


def _tc_to_iso(s: Optional[str]) -> Optional[str]:
    """TC compact date → UTC ISO 8601 `YYYY-MM-DDTHH:MM:SSZ`.

    Нормализует формат TC (`20260512T174418+0000`) к стандартному ISO
    чтобы LLM мог сравнивать с incident.starts_at без путаницы форматов.
    """
    if not s:
        return None
    dt = _parse_tc_date(s)
    if dt is None:
        return s  # нераспознанный формат — возвращаем как есть
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_builds_direct(
    branch: str,
    since: datetime,
    wanted_branches: set[str],
) -> list[dict[str, Any]]:
    """Синхронный запрос к TC REST API через TeamCityClient.
    Запускается в thread pool из async-контекста.
    """
    client = _TCClient(url=settings.TC_URL, token=settings.TC_TOKEN, timeout=settings.TC_TIMEOUT_SECONDS * 2)
    try:
        _build_fields = (
            "build(id,number,status,state,branchName,buildTypeId,startDate,finishDate,"
            "agent(name),statusText,"
            "triggered(type,date,user(username,name)),"
            "lastChanges(change(id,version,username,date,comment,"
            "files(count,file(file,changeType)))))"
        )

        def _list(extra_locator: str = "") -> list[dict[str, Any]]:
            parts = f"affectedProject:(id:{settings.TC_BACKEND_PROJECT_ID}),state:finished,count:50"
            if extra_locator:
                parts += f",{extra_locator}"
            data = client.get_json(
                "/app/rest/builds",
                params={"locator": parts, "fields": _build_fields},
            )
            return [_build_summary_direct(b) for b in data.get("build", [])]

        default_builds = _list()
        branch_builds = _list(f"branch:{branch}")

        seen: set[int] = set()
        merged: list[dict[str, Any]] = []
        for b in default_builds + branch_builds:
            bid = b.get("id")
            if bid is None or bid in seen:
                continue
            seen.add(bid)
            if b.get("branch") not in wanted_branches:
                continue
            finished = _parse_tc_date(b.get("finished", ""))
            if finished is None or finished < since:
                continue
            merged.append(b)

        merged.sort(key=lambda b: b.get("finished", ""), reverse=True)
        builds = merged[:5]

        return builds
    finally:
        client.close()


def _build_summary_direct(b: dict[str, Any]) -> dict[str, Any]:
    """Нормализует build из TC REST API, включая lastChanges → changes[]."""
    url = None
    if settings.TEAMCITY_WEB_URL and b.get("id"):
        url = f"{settings.TEAMCITY_WEB_URL.rstrip('/')}/viewLog.html?buildId={b['id']}"

    raw_changes = (b.get("lastChanges") or {}).get("change", [])
    changes = [
        {
            "version": (c.get("version") or "")[:12],
            "author": c.get("username"),
            "date": _tc_to_iso(c.get("date")),
            "comment": _trim_comment(c.get("comment", "")),
            "files": [
                f.get("file") for f in (c.get("files") or {}).get("file", [])
                if f.get("file") and not (f.get("file") or "").endswith("/")
            ][:20],
        }
        for c in raw_changes
    ]

    # triggered.user — деплойщик (тот кто запустил билд). Может быть None для
    # auto-triggered (vcs / schedule / dependency) — фиксируем тип в `triggered_by_type`.
    triggered = b.get("triggered") or {}
    trig_user = (triggered.get("user") or {})
    return {
        "id": b.get("id"),
        "number": b.get("number"),
        "status": b.get("status"),
        "state": b.get("state"),
        "buildtype_id": b.get("buildTypeId"),
        "branch": b.get("branchName"),
        "started": _tc_to_iso(b.get("startDate")),
        "finished": _tc_to_iso(b.get("finishDate")),
        "agent": (b.get("agent") or {}).get("name"),
        "status_text": b.get("statusText"),
        "url": url,
        "changes": changes,
        "triggered_by": trig_user.get("username") or trig_user.get("name"),
        "triggered_by_type": triggered.get("type"),  # 'user' | 'vcs' | 'schedule' | 'buildDependency' | ...
    }


async def incident_teamcity_context(
    namespace: Optional[str],
    incident_starts_at: Optional[str],
) -> Optional[dict[str, Any]]:
    """Собрать TC-контекст к инциденту.

    Возвращает dict с полями `branch`, `lookback_minutes`, `namespace`,
    `recent_builds` (≤ 5, каждый — c id/status/buildtype/branch/url + changes).
    None — если ни один транспорт не доступен, namespace не маппится, или ошибка.
    """
    if not settings.TC_URL and not settings.TEAMCITY_MCP_URL:
        return None
    branch = branch_for_namespace(namespace)
    if branch is None:
        return None

    try:
        end = (
            datetime.fromisoformat((incident_starts_at or "").replace("Z", "+00:00"))
            if incident_starts_at
            else datetime.now(timezone.utc)
        )
    except Exception:
        end = datetime.now(timezone.utc)
    since = end - timedelta(minutes=settings.TC_LOOKBACK_MINUTES)

    wanted_branches = {branch, f"refs/heads/{branch}"}

    # Путь 1: прямой TC REST API (локальный пакет teamcity_mcp)
    if settings.TC_URL and settings.TC_TOKEN and _TC_CLIENT_AVAILABLE:
        try:
            loop = asyncio.get_event_loop()
            builds = await loop.run_in_executor(
                None, _fetch_builds_direct, branch, since, wanted_branches
            )
            recent_builds = [
                {
                    "id": b.get("id"),
                    "number": b.get("number"),
                    "status": b.get("status"),
                    "state": b.get("state"),
                    "buildtype_id": b.get("buildtype_id"),
                    "branch": b.get("branch"),
                    "started_at": b.get("started"),
                    "finished_at": b.get("finished"),
                    "agent": b.get("agent"),
                    "status_text": b.get("status_text"),
                    "url": b.get("url"),
                    "changes": b.get("changes", []),
                }
                for b in builds
            ]
            return {
                "branch": branch,
                "lookback_minutes": settings.TC_LOOKBACK_MINUTES,
                "namespace": namespace,
                "recent_builds": recent_builds,
            }
        except Exception as e:
            logger.warning("teamcity.direct_client failed, trying MCP fallback", error=str(e))

    # Путь 2: MCP HTTP server (если задан TEAMCITY_MCP_URL)
    if not settings.TEAMCITY_MCP_URL:
        return None

    mcp = McpHttpClient(
        url=settings.TEAMCITY_MCP_URL,
        bearer_token=settings.TEAMCITY_MCP_TOKEN or None,
        timeout=settings.TC_TIMEOUT_SECONDS,
    )
    try:
        async def _list(extra: dict[str, Any]) -> list[dict[str, Any]]:
            try:
                rv = await mcp.call_tool(
                    "teamcity_list_builds",
                    {"project_id": settings.TC_BACKEND_PROJECT_ID, "state": "finished", "count": 50, **extra},
                )
                return rv if isinstance(rv, list) else []
            except (httpx.HTTPError, RuntimeError) as e:
                logger.warning("teamcity.list_builds (mcp) failed", error=str(e), extra=extra)
                return []

        default_branch_builds, logical_branch_builds = await asyncio.gather(
            _list({}), _list({"branch": branch})
        )
        seen_ids: set[int] = set()
        merged: list[dict[str, Any]] = []
        for b in default_branch_builds + logical_branch_builds:
            bid = b.get("id")
            if bid is None or bid in seen_ids:
                continue
            seen_ids.add(bid)
            if b.get("branch") not in wanted_branches:
                continue
            finished = _parse_tc_date(b.get("finished", ""))
            if finished is None or finished < since:
                continue
            merged.append(b)
        merged.sort(key=lambda b: b.get("finished", ""), reverse=True)
        builds = merged[:5]

        async def _changes(b: dict[str, Any]) -> list[dict[str, Any]]:
            btype = b.get("buildtype_id")
            if not btype:
                return []
            try:
                rv = await mcp.call_tool("teamcity_list_changes", {"buildtype_id": btype, "count": 5})
                return rv if isinstance(rv, list) else []
            except (httpx.HTTPError, RuntimeError) as e:
                logger.warning("teamcity.list_changes (mcp) failed", buildtype_id=btype, error=str(e))
                return []

        all_changes = await asyncio.gather(*[_changes(b) for b in builds]) if builds else []

        recent_builds = [
            {
                "id": b.get("id"),
                "number": b.get("number"),
                "status": b.get("status"),
                "state": b.get("state"),
                "buildtype_id": b.get("buildtype_id"),
                "branch": b.get("branch"),
                "started_at": b.get("started"),
                "finished_at": b.get("finished"),
                "agent": b.get("agent"),
                "status_text": b.get("status_text"),
                "url": (
                    f"{settings.TEAMCITY_WEB_URL.rstrip('/')}/viewLog.html?buildId={b.get('id')}"
                    if settings.TEAMCITY_WEB_URL else None
                ),
                "changes": [
                    {
                        "version": (c.get("version") or "")[:12],
                        "author": c.get("username") or c.get("author"),
                        "date": c.get("date"),
                        "comment": _trim_comment(c.get("comment", "")),
                    }
                    for c in changes
                ],
            }
            for b, changes in zip(builds, all_changes)
        ]
        return {
            "branch": branch,
            "lookback_minutes": settings.TC_LOOKBACK_MINUTES,
            "namespace": namespace,
            "recent_builds": recent_builds,
        }
    finally:
        await mcp.aclose()


def teamcity_context_to_prompt(tc_ctx: Optional[dict]) -> str:
    """Форматирует TC-контекст для инжекта в LLM-промпт hypothesis-стадии."""
    if not tc_ctx or not tc_ctx.get("recent_builds"):
        return ""
    branch = tc_ctx.get("branch", "?")
    lookback = tc_ctx.get("lookback_minutes", 60)
    lines = [f"=== TEAMCITY DEPLOYS ({branch}, lookback {lookback} min) ==="]
    for b in tc_ctx["recent_builds"]:
        num = b.get("number", "?")
        btype = (b.get("buildtype_id") or "").split("_")[-1]  # short name
        status = b.get("status", "?")
        finished = (b.get("finished_at") or "")[:19]  # YYYY-MM-DDTHH:MM:SS
        lines.append(f"Build #{num} — {btype} — {status} — {finished}Z (UTC)")
        for c in b.get("changes") or []:
            rev = (c.get("version") or "")[:8]
            author = c.get("author") or c.get("username") or "?"
            comment = c.get("comment") or ""
            lines.append(f"  [{rev}] {author}: {comment}")
            files = c.get("files") or []
            if files:
                lines.append(f"  Files ({len(files)}): {', '.join(files[:10])}")
                if len(files) > 10:
                    lines.append(f"         (+{len(files)-10} more)")
        if b.get("url"):
            lines.append(f"  URL: {b['url']}")
    return "\n".join(lines)
