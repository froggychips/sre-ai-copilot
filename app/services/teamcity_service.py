"""TeamCity REST client → recent-deploys context для incident-а.

Цель: при инциденте в k8s-namespace (preprod/prod/preupdate/squad-gd) подтянуть
последние законченные билды соответствующей ветки в backend-проекте + список
изменений (commits, авторы), чтобы analyzer/hypothesis видели «что катилось».

Никакого write — только GET. При недоступности/таймауте TC — graceful degrade
(возвращаем None, инцидент обрабатывается без контекста).
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
import structlog

from app.config import settings

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


def _fmt_tc_date(d: datetime) -> str:
    """TC ожидает `yyyyMMdd'T'HHmmss±HHmm` без двоеточий и микросекунд."""
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.strftime("%Y%m%dT%H%M%S%z")


class TeamCityClient:
    """Тонкая обёртка над TC REST API. Auth — Bearer TC_TOKEN."""

    def __init__(self, base_url: str, token: str, timeout: float = 5.0):
        self._base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_builds(
        self,
        project_id: str,
        branch: Optional[str] = None,
        since: Optional[datetime] = None,
        count: int = 5,
    ) -> list[dict[str, Any]]:
        """Last `count` finished builds в проекте (рекурсивно).

        `branch` фильтруется на клиенте: TC хранит ветки по-разному (логическое имя
        для buildTypes с branchSpec, голый `refs/heads/<x>` для buildTypes без него),
        и одним locator-ом покрыть оба случая нельзя. Запрашиваем `default:any` +
        фильтруем результат.
        """
        wanted = {branch, f"refs/heads/{branch}", "<default>"} if branch else None
        fields = "build(id,number,status,state,branchName,startDate,finishDate,buildTypeId,buildType(name,projectName),triggered(user(name)))"

        async def _query(extra: list[str], page_count: int) -> list[dict[str, Any]]:
            locator = [
                f"affectedProject:(id:{project_id})",
                f"count:{page_count}",
                "state:finished",
            ] + extra
            if since:
                locator.append(f"finishDate:(date:{_fmt_tc_date(since)},condition:after)")
            r = await self._client.get(
                "/app/rest/builds",
                params={"locator": ",".join(locator), "fields": fields},
            )
            r.raise_for_status()
            return r.json().get("build", [])

        # Стратегия 1: server-side branch filter работает для buildTypes с branchSpec
        # (TC мапит логическое имя ветки). Быстро, отдельный запрос.
        builds: list[dict[str, Any]] = []
        if branch:
            try:
                builds = await _query([f"branch:(name:{branch},default:any)"], count)
            except httpx.HTTPError as e:
                logger.warning("teamcity.list_builds branch-locator failed, falling back", error=str(e))
                builds = []

        # Стратегия 2: если ничего не нашлось — выгребаем widely и фильтруем на клиенте.
        # Покрывает buildTypes без branchSpec, где branchName = `refs/heads/<branch>`.
        if not builds and wanted:
            wide = await _query(["branch:default:any"], 500)
            builds = [b for b in wide if b.get("branchName") in wanted]

        # Без branch — обычный путь
        if not builds and not branch:
            builds = await _query(["branch:default:any"], count)

        return builds[:count]

    async def list_build_changes(self, build_id: int, count: int = 20) -> list[dict[str, Any]]:
        """Изменения, попавшие в build."""
        params = {
            "locator": f"build:(id:{build_id}),count:{count}",
            "fields": "change(id,version,username,date,comment)",
        }
        r = await self._client.get("/app/rest/changes", params=params)
        r.raise_for_status()
        return r.json().get("change", [])


def _trim_comment(c: str, n: int = 140) -> str:
    c = (c or "").strip().splitlines()[0] if c else ""
    return c[: n - 1] + "…" if len(c) > n else c


async def incident_teamcity_context(
    namespace: Optional[str],
    incident_starts_at: Optional[str],
) -> Optional[dict[str, Any]]:
    """Собрать TC-контекст к инциденту.

    Возвращает dict вида:
        {
            "branch": "preprod",
            "lookback_minutes": 60,
            "recent_builds": [
                {"id": 115140, "status": "SUCCESS", "buildtype": "Build and update preprod + squad-gd",
                 "branch": "preprod", "finished_at": "...", "url": "...",
                 "changes": [{"author": "ivan", "comment": "fix login", "date": "..."}, ...]},
                ...
            ]
        }

    None — если TC не настроен, ветка не выводится, или TC недоступен.
    """
    if not settings.TC_URL or not settings.TC_TOKEN:
        return None
    branch = branch_for_namespace(namespace)
    if branch is None:
        return None

    # окно поиска: lookback до момента started_at инцидента (если есть), иначе до now
    try:
        # AlertManager отдаёт RFC3339; допускаем разные варианты
        end = datetime.fromisoformat((incident_starts_at or "").replace("Z", "+00:00")) if incident_starts_at else datetime.now(timezone.utc)
    except Exception:
        end = datetime.now(timezone.utc)
    since = end - timedelta(minutes=settings.TC_LOOKBACK_MINUTES)

    client = TeamCityClient(settings.TC_URL, settings.TC_TOKEN, settings.TC_TIMEOUT_SECONDS)
    try:
        try:
            builds = await client.list_builds(
                project_id=settings.TC_BACKEND_PROJECT_ID,
                branch=branch,
                since=since,
                count=5,
            )
        except httpx.HTTPError as e:
            logger.warning("teamcity.list_builds failed", error=str(e), namespace=namespace, branch=branch)
            return None

        # changes — параллельно для всех найденных билдов, но мягко: ошибки игнорируем
        async def _changes(b: dict[str, Any]) -> list[dict[str, Any]]:
            try:
                return await client.list_build_changes(int(b["id"]))
            except httpx.HTTPError as e:
                logger.warning("teamcity.list_changes failed", build_id=b.get("id"), error=str(e))
                return []

        all_changes = await asyncio.gather(*[_changes(b) for b in builds]) if builds else []

        recent_builds = []
        for b, changes in zip(builds, all_changes):
            recent_builds.append({
                "id": b.get("id"),
                "number": b.get("number"),
                "status": b.get("status"),
                "buildtype_id": b.get("buildTypeId"),
                "buildtype": (b.get("buildType") or {}).get("name"),
                "branch": b.get("branchName"),
                "started_at": b.get("startDate"),
                "finished_at": b.get("finishDate"),
                "triggered_by": ((b.get("triggered") or {}).get("user") or {}).get("name"),
                "url": f"{settings.TC_URL}/viewLog.html?buildId={b.get('id')}",
                "changes": [
                    {
                        "version": c.get("version", "")[:12],
                        "author": c.get("username"),
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
        await client.aclose()
