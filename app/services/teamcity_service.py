"""TeamCity → recent-deploys context для incident-а, через mcp-teamcity-server.

Вместо прямого REST API теперь ходим через MCP-сервер
(`external/mcp/teamcity-server` — см. MR !1). Это даёт полный набор 13 тулов
(вместо 4 в tools-server) и единый auth-контур с остальными WO MCP-серверами.

Публичный интерфейс `incident_teamcity_context()` не изменился — потребители
(webhook ingestion → analyzer/hypothesis prompts) остаются без правок.

Никакого write — только `teamcity_list_builds` + `teamcity_list_changes`.
При недоступности MCP — graceful degrade (None).
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
import structlog

from app.config import settings
from app.services.mcp_client import McpHttpClient

logger = structlog.get_logger()

# k8s-namespace → логическое имя ветки в TC. Покрывает основные prod-сценарии WO.
# squad-N (любое число) намеренно вернёт None: на squad деплоят с разных feature-веток
# вручную, корреляция «текущий push в default → деплой» не работает.
_BRANCH_RULES = [
    (re.compile(r"^prod(-|$)"), "prod"),
    (re.compile(r"^preprod(-|$)"), "preprod"),
    (re.compile(r"^preupdate(-|$)"), "preupdate"),
    (re.compile(r"^squad-gd(-|$)"), "preprod"),  # WO-11324: squad-gd деплоится из preprod-ветки
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


async def incident_teamcity_context(
    namespace: Optional[str],
    incident_starts_at: Optional[str],
) -> Optional[dict[str, Any]]:
    """Собрать TC-контекст к инциденту через mcp-teamcity-server.

    Возвращает dict с полями `branch`, `lookback_minutes`, `namespace`,
    `recent_builds` (≤ 5, каждый — c id/status/buildtype/branch/url + changes).
    None — если MCP не сконфигурирован, namespace не маппится, MCP недоступен.
    """
    if not settings.TEAMCITY_MCP_URL:
        return None
    branch = branch_for_namespace(namespace)
    if branch is None:
        return None

    try:
        end = datetime.fromisoformat((incident_starts_at or "").replace("Z", "+00:00")) if incident_starts_at else datetime.now(timezone.utc)
    except Exception:
        end = datetime.now(timezone.utc)
    since = end - timedelta(minutes=settings.TC_LOOKBACK_MINUTES)

    # Принятые ограничения mcp-teamcity-server v1 (MR !1):
    #   1. teamcity_list_builds НЕ умеет default:any в branch locator —
    #      одним вызовом покрыть и buildTypes с branchSpec (branch=`prod`),
    #      и без него (branch=`refs/heads/preprod`) нельзя. Делаем 2 запроса:
    #      без branch (default-ветки) + с branch=<logical> и объединяем.
    #   2. teamcity_list_changes не принимает build_id. Берём общий поток коммитов
    #      по buildtype_id — это «recent changes in this pipeline» (близко к
    #      «changes in this build», достаточно для корреляции в analyzer-prompt-е).
    wanted_branches = {branch, f"refs/heads/{branch}"}
    mcp = McpHttpClient(
        url=settings.TEAMCITY_MCP_URL,
        bearer_token=settings.TEAMCITY_MCP_TOKEN or None,
        timeout=settings.TC_TIMEOUT_SECONDS,
    )
    try:
        async def _list(extra: dict[str, Any]) -> list[dict[str, Any]]:
            try:
                rv = await mcp.call_tool("teamcity_list_builds", {
                    "project_id": settings.TC_BACKEND_PROJECT_ID,
                    "state": "finished",
                    "count": 50,
                    **extra,
                })
                return rv if isinstance(rv, list) else []
            except (httpx.HTTPError, RuntimeError) as e:
                logger.warning("teamcity.list_builds (mcp) failed", error=str(e), extra=extra)
                return []

        default_branch, logical_branch = await asyncio.gather(
            _list({}),                      # default branches (refs/heads/<x> для buildTypes без branchSpec)
            _list({"branch": branch}),      # logical name (для buildTypes с branchSpec)
        )

        seen_ids: set[int] = set()
        merged: list[dict[str, Any]] = []
        for b in default_branch + logical_branch:
            bid = b.get("id")
            if bid in seen_ids:
                continue
            seen_ids.add(bid)
            if b.get("branch") not in wanted_branches:
                continue
            finished = _parse_tc_date(b.get("finished", ""))
            if finished is None or finished < since:
                continue
            merged.append(b)
        # Сортируем по дате (свежее первым)
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

        recent_builds = []
        for b, changes in zip(builds, all_changes):
            recent_builds.append({
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
            })

        return {
            "branch": branch,
            "lookback_minutes": settings.TC_LOOKBACK_MINUTES,
            "namespace": namespace,
            "recent_builds": recent_builds,
        }
    finally:
        await mcp.aclose()
