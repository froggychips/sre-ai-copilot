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

# Пакет teamcity_mcp:
#   1. Если установлен системно (pip install teamcity-mcp / dev TC_MCP_SRC env)
#      — используем его.
#   2. Иначе — vendor-копия в app.vendor.teamcity_mcp (минимум, только client.py).
# Vendor нужен потому что teamcity-mcp пока не публикуется на PyPI / git remote,
# а direct TC REST API в проде должен работать без зависимости от dev-машины.
_tc_mcp_src = os.environ.get("TC_MCP_SRC", "")
if _tc_mcp_src and _tc_mcp_src not in sys.path:
    sys.path.insert(0, _tc_mcp_src)
try:
    from teamcity_mcp.client import TeamCityClient as _TCClient
    _TC_CLIENT_AVAILABLE = True
    _TC_CLIENT_SOURCE = "external"
except ImportError:
    try:
        from app.vendor.teamcity_mcp.client import TeamCityClient as _TCClient
        _TC_CLIENT_AVAILABLE = True
        _TC_CLIENT_SOURCE = "vendor"
    except ImportError:
        _TC_CLIENT_AVAILABLE = False
        _TC_CLIENT_SOURCE = "none"

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


def is_prod_buildtype(buildtype_id: Optional[str]) -> bool:
    """Конфиг из прод-проекта TC (`..._Prod_...` / `..._Prod`).

    Окружение прод-джобы задаёт ПРОЕКТ, а не VCS-ветка: в WO прод-конфиги
    (`Wo_Backend_K8sNewCluster_Prod_*`, `Wo_StaticsNewCluster_Prod_*`)
    запускаются на ветке preprod — оттуда берётся только инструментарий
    (скрипты wo-k8s), а деплой уходит в prod дочерним билдом с branchName=prod.
    Наивная атрибуция по ветке относила их к preprod-* и squad-gd-*
    (у squad-gd правило ветки — тоже preprod), оставляя prod-* без истории.
    """
    bt = buildtype_id or ""
    return "_Prod_" in bt or bt.endswith("_Prod")


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
    """Парсит дату в tz-aware datetime. Принимает два формата:

      1. TC compact `yyyyMMdd'T'HHmmss±HHmm` (напр. `20260512T174418+0000`)
         — сырой формат TC REST API.
      2. ISO 8601 `YYYY-MM-DDTHH:MM:SSZ` (напр. `2026-05-12T17:44:18Z`)
         — уже нормализованный _tc_to_iso. Обязателен потому что merge/filter
         (_fetch_builds_direct / MCP-путь) re-парсят поле `finished`, которое
         _build_summary_direct кладёт УЖЕ в ISO. Без ISO-фолбэка каждый build
         дропался (finished=None) и «recent deploys» всегда пустой.

    Возвращает None если формат не распознан.
    """
    if not s:
        return None
    # 1) сырой compact TC — strptime уже даёт tz-aware (±HHmm).
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%S%z")
    except ValueError:
        pass
    # 2) ISO 8601 — fromisoformat не ест `Z` до 3.11, приводим к +00:00.
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    # naive ISO (без tz) → считаем UTC, чтобы сравнение с tz-aware `since` не падало.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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


# ── recent deploys (для cluster-wide дайджеста) ─────────────────────────────
#
# В отличие от incident_teamcity_context (поиск вокруг конкретного timestamp,
# узкое окно), здесь нужно: «кто что катил за последние N часов».
# Используется в Daily Cluster Digest. Per-helm-release lookup не работает —
# TC устроен «buildtype per pipeline action» (Build and update / Kingdom
# deploy / Shared deploy / Backup all db), не «buildtype per service».
# Поэтому показываем top deploy-builds глобально.

_DEPLOY_NAME_TOKENS = ("deploy", "update", "backup")
_DEPLOY_NAME_EXCLUDE = ("set client min", "set ab test", "update terrain",
                        "update secret", "delete namespace")

# Пагинация TC REST. Раньше был один запрос `count:200`: TC отдавал 200 САМЫХ
# СВЕЖИХ билдов проекта (включая не-deploy), и только потом Python-фильтр по
# имени buildType выкидывал лишнее — т.е. кап съедали чужие билды.
# `backfill_tc_deploys --days 30 --limit 1000` реально получал ≤200 сырых
# билдов на проект и молча терял хвост истории. Теперь идём страницами
# (`count:N,start:M` — постраничный локатор TC REST), достижение капа логируем.
#
# Фильтр по deploy-имени НАМЕРЕННО остался в Python: локатор TC не умеет
# substring-матч по buildType.name (только точное имя/id), а вердикт
# `_is_deploy_buildtype_name` — эвристика из токенов + исключений
# («Update terrain»/«Set ab test» не деплои). Перечислять buildType-ы
# явно в локаторе = ручной whitelist, который протухнет с первым новым
# пайплайном. Кап при этом больше не съедается: страницы идут до
# исчерпания окна, а не до 200 сырых билдов.
_TC_PAGE_SIZE = 200
_TC_MAX_PAGES = 10  # ≤2000 сырых билдов на проект за окно


def _is_deploy_buildtype_name(name: Optional[str]) -> bool:
    if not name:
        return False
    lower = name.lower()
    if any(ex in lower for ex in _DEPLOY_NAME_EXCLUDE):
        return False
    return any(tok in lower for tok in _DEPLOY_NAME_TOKENS)


def _fetch_recent_deploys_direct(
    project_id: str,
    since: datetime,
    limit: int,
) -> list[dict[str, Any]]:
    """Sync TC REST: finished builds в проекте с `triggered.user` и `buildType.name`.

    Запускается в thread pool из async-контекста. Идёт постранично
    (`count:_TC_PAGE_SIZE,start:…`) до исчерпания окна `since` либо капа
    _TC_MAX_PAGES; фильтр по deploy-name — на стороне Python (см. комментарий
    у _TC_PAGE_SIZE). Достижение капа логируется warning-ом.
    """
    client = _TCClient(url=settings.TC_URL, token=settings.TC_TOKEN,
                       timeout=settings.TC_TIMEOUT_SECONDS * 2)
    try:
        # revisions(...) даёт commit SHA каждого VCS root в билде. Нужен для
        # корреляции «новый коммит → новый инцидент» в kg_deployments.sha.
        # properties: НАСТОЯЩАЯ цель деплоя. Атрибуция по VCS-ветке врала —
        # замер 22.08.2026: `BuildAndDeploy #2917` деплоил squad-1, а в графе
        # оказался записан на preprod-* и squad-gd-*, причём сервисов squad-1
        # среди 596 записей не было НИ ОДНОГО. Ветка у таких конфигов
        # литеральный `<default>`, из неё окружение не выводится.
        #
        # В параметрах билда лежит точный ответ: NAMESPACE есть у всех
        # deploy-конфигов, SERVICE_NAME — у тех, что деплоят один сервис
        # (`MigrateAndUpdateService`, часть `OneServiceBuildAndUpdate`).
        fields = (
            "build(id,number,status,state,branchName,buildTypeId,"
            "buildType(name),startDate,finishDate,"
            "properties(property(name,value)),"
            "triggered(type,date,user(username,name)),"
            "revisions(revision(version,vcs-root-instance(name,vcs-root-id))))"
        )
        # TC sinceDate format: yyyyMMdd'T'HHmmss±HHmm
        since_str = since.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S+0000")

        out: list[dict[str, Any]] = []
        scanned = 0
        for page in range(_TC_MAX_PAGES):
            locator = (
                f"affectedProject:(id:{project_id}),"
                f"state:finished,sinceDate:{since_str},"
                f"count:{_TC_PAGE_SIZE},start:{page * _TC_PAGE_SIZE}"
            )
            try:
                data = client.get_json("/app/rest/builds",
                                       params={"locator": locator, "fields": fields})
            except Exception:
                if page == 0:
                    raise  # первая страница не поднялась — это отказ проекта
                # Дальше по истории: отдаём собранное, но не молча.
                logger.warning(
                    "teamcity.recent_deploys_page_failed",
                    project=project_id, page=page, collected=len(out),
                    exc_info=True,
                )
                break
            raw = data.get("build", []) or []
            scanned += len(raw)

            for b in raw:
                btype_name = (b.get("buildType") or {}).get("name")
                props = {
                    pr.get("name"): pr.get("value")
                    for pr in ((b.get("properties") or {}).get("property") or [])
                    if pr.get("name")
                }
                if not _is_deploy_buildtype_name(btype_name):
                    continue
                triggered = b.get("triggered") or {}
                trig_user = triggered.get("user") or {}
                # revisions: для monorepo может быть несколько VCS root.
                # Основной SHA — первый, полный список идёт в all_revisions.
                raw_revs = (b.get("revisions") or {}).get("revision", []) or []
                all_revs: list[dict[str, Any]] = []
                for r in raw_revs:
                    ver = r.get("version")
                    if not ver:
                        continue
                    vri = r.get("vcs-root-instance") or {}
                    all_revs.append({
                        "sha": ver,
                        "root": vri.get("name"),
                        "vcs_root_id": vri.get("vcs-root-id") or vri.get("vcsRootId"),
                    })
                sha = all_revs[0]["sha"] if all_revs else None
                out.append({
                    "id": b.get("id"),
                    "number": b.get("number"),
                    "status": b.get("status"),
                    "branch": b.get("branchName"),
                    "buildtype_id": b.get("buildTypeId"),
                    "buildtype_name": btype_name,
                    "started_at": _tc_to_iso(b.get("startDate")),
                    "finished_at": _tc_to_iso(b.get("finishDate")),
                    "triggered_by": trig_user.get("username") or trig_user.get("name"),
                    "triggered_type": triggered.get("type"),
                    "sha": sha,
                    "all_revisions": all_revs,
                    # Точная цель деплоя из параметров билда. NAMESPACE тут —
                    # РЕАЛЬМ («squad-27», «preprod»), а не полное имя
                    # namespace: разворачивать его в конкретные namespace —
                    # дело вызывающего.
                    "target_realm": props.get("NAMESPACE") or None,
                    "target_service": props.get("SERVICE_NAME") or None,
                    "url": (
                        f"{settings.TEAMCITY_WEB_URL.rstrip('/')}/viewLog.html?buildId={b.get('id')}"
                        if settings.TEAMCITY_WEB_URL and b.get("id") else None
                    ),
                })

            if len(raw) < _TC_PAGE_SIZE:
                break  # неполная страница → билды в окне исчерпаны
        else:
            # Кап страниц исчерпан — хвост окна не просмотрен. Для backfill
            # это значит «истории меньше, чем в TC», о чём надо знать.
            logger.warning(
                "teamcity.recent_deploys_page_cap_reached",
                project=project_id, pages=_TC_MAX_PAGES,
                scanned=scanned, deploys=len(out), since=since_str,
            )

        out.sort(key=lambda x: x.get("finished_at") or "", reverse=True)
        return out[:limit]
    finally:
        client.close()


def _tc_project_ids() -> list[str]:
    """G3.1: список TC projects для sync. TC_PROJECT_IDS (CSV) приоритетнее,
    fallback на TC_BACKEND_PROJECT_ID для обратной совместимости."""
    csv = (settings.TC_PROJECT_IDS or "").strip()
    if csv:
        return [p.strip() for p in csv.split(",") if p.strip()]
    if settings.TC_BACKEND_PROJECT_ID:
        return [settings.TC_BACKEND_PROJECT_ID]
    return []


async def recent_deploys(
    *,
    lookback_hours: int = 24,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Top-N deploy-builds (finished) за последние `lookback_hours` часов.

    G3.1: итерирует все TC projects из `_tc_project_ids()` (TC_PROJECT_IDS
    CSV → fallback TC_BACKEND_PROJECT_ID). Объединяет результаты,
    сортирует по finished_at DESC, режет до limit. `limit` применяется к
    общему списку, не к каждому project (избегаем 1k-build flood).

    Каждый dict: id/number/status/branch/buildtype_id/buildtype_name/
    started_at/finished_at/triggered_by/triggered_type/url. Пустой
    список — если TC не настроен / direct client недоступен / ошибка.

    ВАЖНО про пустой список: он неразличим для вызывающего («деплоев не
    было» vs «источник ослеп»), поэтому КАЖДАЯ причина пустоты логируется
    явно. Инцидент 2026-08-11: поток деплоев стоял с 10.08, а
    `deploy_stream_ingestion` и Discord-атрибуция молча трактовали это как
    «деплоев не было» — прод-релиз получил в алерте пометку «вряд ли
    связано с деплоем» через 20 секунд после раскатки. Диагноз по логам
    был невозможен: тихий `return []` не оставлял следа.
    Машиночитаемый статус конфигурации — `tc_sync_config_status()`.
    """
    cfg = tc_sync_config_status()
    if not cfg["configured"]:
        logger.warning(
            "teamcity.recent_deploys_not_configured",
            reason=cfg["reason"],
            client_source=_TC_CLIENT_SOURCE,
        )
        return []
    projects = _tc_project_ids()
    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    loop = asyncio.get_event_loop()
    combined: list[dict[str, Any]] = []
    failed: list[str] = []
    for pid in projects:
        try:
            project_builds = await loop.run_in_executor(
                None, _fetch_recent_deploys_direct, pid, since, limit,
            )
            combined.extend(project_builds)
        except Exception as e:
            failed.append(pid)
            logger.warning(
                "teamcity.recent_deploys_failed project=%s error=%s",
                pid, str(e),
            )
    # Все проекты отвалились — это отказ источника, а не тихий период.
    # Без отдельной ветки такой случай выглядел как «деплоев не было»:
    # per-project warning тонул в логах воркера (там же metrics_sync).
    if failed and not combined:
        logger.error(
            "teamcity.recent_deploys_all_projects_failed",
            projects=projects,
            failed=failed,
            lookback_hours=lookback_hours,
        )
    elif not combined:
        logger.info(
            "teamcity.recent_deploys_empty",
            projects=projects,
            lookback_hours=lookback_hours,
        )
    combined.sort(key=lambda x: x.get("finished_at") or "", reverse=True)
    return combined[:limit]


def tc_sync_config_status() -> dict[str, Any]:
    """Настроен ли pull деплоев из TC. `{configured: bool, reason: str}`.

    Нужен, чтобы отличать «источник не настроен» (ожидаемая пустота, не
    инцидент) от «настроен, но отдаёт 0» (ослепший синк — инцидент).
    До этого обе ситуации давали одинаковый пустой список, и watchdog
    `check_deploy_stream_ingestion` в обеих отвечал ok.
    """
    if not settings.TC_URL:
        return {"configured": False, "reason": "TC_URL пуст"}
    if not settings.TC_TOKEN:
        return {"configured": False, "reason": "TC_TOKEN пуст"}
    if not _TC_CLIENT_AVAILABLE:
        return {
            "configured": False,
            "reason": f"TC-клиент не импортирован (source={_TC_CLIENT_SOURCE})",
        }
    if not _tc_project_ids():
        return {
            "configured": False,
            "reason": "ни TC_PROJECT_IDS, ни TC_BACKEND_PROJECT_ID не заданы",
        }
    return {"configured": True, "reason": ""}
