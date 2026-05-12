"""GitLab API — обогащение MR-метаданными по SHA коммитов из TC-деплоев.

Цепочка: TC-билд → changes[].version (sha) → MR title/url/author/files.
Это даёт прямой ответ на «какой PR вызвал проблему» — сильнейший сигнал
для гипотез recent_deploy и root cause.

Активен если GITLAB_URL + GITLAB_TOKEN заданы. Graceful degrade при ошибке.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Лимиты — не хотим вешать pipeline на GitLab API при большом числе коммитов.
_MAX_SHAS = 10       # коммитов из TC берём не более N
_MAX_MRS = 5         # MR из найденных — не более N (сортируем по merged_at desc)
_MAX_FILES = 20      # файлов на MR


class GitLabClient:
    def __init__(self, base_url: str, token: str, timeout: float = 8.0) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"PRIVATE-TOKEN": token}
        self._timeout = timeout

    async def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        url = f"{self._base}/api/v4{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.get(url, headers=self._headers, params=params or {})
            r.raise_for_status()
            return r.json()

    async def resolve_project_id(self, project_path: str) -> Optional[int]:
        """Преобразовать 'new-wo/backend-services' → числовой project_id."""
        try:
            data = await self._get(f"/projects/{project_path.replace('/', '%2F')}")
            return data.get("id")
        except Exception as e:
            logger.debug("gitlab.resolve_project_id failed path=%r: %s", project_path, e)
            return None

    async def mrs_for_sha(self, project_id: int, sha: str) -> List[Dict[str, Any]]:
        """MR-ы, содержащие этот коммит."""
        try:
            return await self._get(
                f"/projects/{project_id}/repository/commits/{sha}/merge_requests",
                params={"state": "merged", "per_page": 5},
            )
        except Exception as e:
            logger.debug("gitlab.mrs_for_sha sha=%s: %s", sha[:8], e)
            return []

    async def mrs_merged_in_window(
        self,
        project_id: int,
        target_branch: str,
        since: str,
        until: str,
    ) -> List[Dict[str, Any]]:
        """MR-ы смёрженные в target_branch в окне [since, until].

        GitLab self-hosted может не поддерживать order_by=merged_at и
        параметры merged_after/merged_before — фильтруем на клиенте.
        """
        try:
            all_mrs = await self._get(
                f"/projects/{project_id}/merge_requests",
                params={
                    "state": "merged",
                    "target_branch": target_branch,
                    "order_by": "updated_at",
                    "sort": "desc",
                    "per_page": 25,  # берём с запасом, фильтруем на клиенте
                },
            )
        except Exception as e:
            logger.debug("gitlab.mrs_merged_in_window branch=%s: %s", target_branch, e)
            return []

        # Клиентская фильтрация по merged_at
        filtered = []
        for mr in all_mrs:
            mat = mr.get("merged_at") or ""
            if since <= mat <= until:
                filtered.append(mr)
        return filtered[:_MAX_MRS]

    async def mr_diff_files(self, project_id: int, mr_iid: int) -> List[str]:
        """Список изменённых файлов в MR."""
        try:
            data = await self._get(
                f"/projects/{project_id}/merge_requests/{mr_iid}/changes"
            )
            changes = data.get("changes") or []
            return [c["new_path"] for c in changes[:_MAX_FILES] if c.get("new_path")]
        except Exception as e:
            logger.debug("gitlab.mr_diff_files mr_iid=%d: %s", mr_iid, e)
            return []


async def enrich_with_gitlab(tc_context: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Получить MR-метаданные по window merged_at из TC-контекста.

    Стратегия: TC знает branch (preprod/prod/preupdate) и lookback_minutes.
    Ищем MR смёрженные в эту ветку за lookback-окно до newest finished_at.
    Это надёжнее SHA-lookup, т.к. SHA из TC — коммиты с feature-веток,
    а не merge-коммиты (GitLab squash/merge создаёт новый SHA).

    Возвращает dict:
        mrs: list[{iid, title, url, author, merged_at, files: list[str]}]
        project_url: str
        branch: str
    Или None если GitLab не настроен / нет данных / ошибка.
    """
    if not settings.GITLAB_URL or not settings.GITLAB_TOKEN:
        return None
    if not tc_context:
        return None

    branch = tc_context.get("branch")
    lookback = tc_context.get("lookback_minutes", 60)
    if not branch:
        return None

    # Окно: oldest finished_at из билдов → newest + небольшой запас
    def _to_iso(raw: str) -> str:
        """Нормализует TC-формат '20260512T182755+0000' → ISO '2026-05-12T18:27:55+00:00'."""
        from datetime import datetime
        for fmt in ("%Y%m%dT%H%M%S%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return datetime.strptime(raw.replace("Z", "+0000"), fmt).isoformat()
            except ValueError:
                continue
        return raw  # уже ISO или неизвестный формат

    builds = tc_context.get("recent_builds") or []
    raw_times = [b.get("finished_at") or b.get("started_at") for b in builds if b.get("finished_at") or b.get("started_at")]
    finished_times = [_to_iso(t) for t in raw_times if t]

    if finished_times:
        newest = max(finished_times)
        oldest = min(finished_times)
    else:
        # Если нет билдов — берём окно lookback назад от сейчас
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        newest = now.isoformat()
        oldest = (now - timedelta(minutes=lookback)).isoformat()

    gl = GitLabClient(settings.GITLAB_URL, settings.GITLAB_TOKEN)
    project_id = await gl.resolve_project_id(settings.GITLAB_BACKEND_PROJECT)
    if not project_id:
        logger.warning("gitlab.enrich: could not resolve project_id for %r", settings.GITLAB_BACKEND_PROJECT)
        return None

    mrs = await gl.mrs_merged_in_window(
        project_id=project_id,
        target_branch=branch,
        since=oldest,
        until=newest,
    )

    project_url = f"{settings.GITLAB_URL}/{settings.GITLAB_BACKEND_PROJECT}"
    if not mrs:
        return {"mrs": [], "project_url": project_url, "branch": branch}

    # Для каждого MR получаем список файлов. Параллельно.
    files_list = await asyncio.gather(
        *[gl.mr_diff_files(project_id, mr["iid"]) for mr in mrs],
        return_exceptions=True,
    )

    mrs_out = []
    for mr, files in zip(mrs, files_list):
        author = (mr.get("author") or {}).get("name") or (mr.get("author") or {}).get("username")
        mrs_out.append({
            "iid": mr.get("iid"),
            "title": mr.get("title"),
            "url": mr.get("web_url"),
            "author": author,
            "merged_at": mr.get("merged_at"),
            "files": files if isinstance(files, list) else [],
        })

    return {"mrs": mrs_out, "project_url": project_url, "branch": branch}


def gitlab_context_to_prompt(gl_ctx: Optional[Dict[str, Any]]) -> str:
    """Форматирует GitLab-контекст для инжекта в LLM-промпт."""
    if not gl_ctx or not gl_ctx.get("mrs"):
        return ""
    lines = ["=== GITLAB MRs (merged before incident) ==="]
    for mr in gl_ctx["mrs"]:
        lines.append(f"MR !{mr['iid']} — {mr['title']}")
        lines.append(f"  Author: {mr['author']} | Merged: {(mr['merged_at'] or '')[:16]}")
        if mr["files"]:
            lines.append(f"  Files: {', '.join(mr['files'][:10])}")
            if len(mr["files"]) > 10:
                lines.append(f"         (+{len(mr['files'])-10} more)")
        lines.append(f"  URL: {mr['url']}")
    return "\n".join(lines)
