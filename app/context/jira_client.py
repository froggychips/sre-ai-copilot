"""Atlassian Jira REST API client для обогащения диагностического контекста.

Ищет открытые/недавно закрытые тикеты по сервису чтобы FixAgent знал:
  - OPEN issue  → "known ongoing issue" → рекомендовать link + escalation, не restart
  - RESOLVED issue (< 30 дней) → "recently fixed" → annotate как potential recurrence

Конфигурация (app/config.py):
    JIRA_BASE_URL   — https://org.atlassian.net  (пусто = Jira отключена)
    JIRA_EMAIL      — email для Basic Auth
    JIRA_API_TOKEN  — Personal API Token
    JIRA_PROJECT_KEY — "WO"
    JIRA_BACKEND_LABEL — "backend"
    JIRA_SEARCH_DAYS   — 30
"""
from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# statusCategory.key → canonical open/resolved
_STATUS_OPEN = {"new", "indeterminate"}
_STATUS_RESOLVED = {"done"}


def _jira_status(status_category_key: str) -> str:
    if status_category_key in _STATUS_RESOLVED:
        return "resolved"
    return "open"


class JiraClient:
    """Тонкая обёртка над Jira REST API v3 /search."""

    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        project_key: str = "WO",
        backend_label: str = "backend",
        timeout: float = 8.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._project = project_key
        self._label = backend_label
        self._timeout = timeout
        # Basic Auth: base64("email:token")
        creds = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {creds}",
            "Accept": "application/json",
        }

    async def search_by_service(
        self,
        service: str,
        namespace: Optional[str] = None,
        days: int = 30,
    ) -> List[Dict[str, Any]]:
        """Поиск тикетов по имени сервиса.

        JQL: project = {project} AND labels = "{label}" AND summary ~ "{service}"
             AND created >= "-{days}d" ORDER BY created DESC

        Возвращает список:
            key          str   — "WO-1234"
            summary      str
            status       str   — "open" | "resolved"
            status_name  str   — оригинальное имя статуса
            priority     str   — "Highest" / "Medium" / ...
            url          str   — ссылка на тикет
            created      str   — ISO datetime
        """
        jql_parts = [
            f'project = "{self._project}"',
            f'labels = "{self._label}"',
            f'summary ~ "{service}"',
            f'created >= "-{days}d"',
        ]
        jql = " AND ".join(jql_parts) + " ORDER BY created DESC"

        # Atlassian deprecated GET /rest/api/3/search (returns 410 Gone).
        # Current endpoint: POST /rest/api/3/search/jql
        payload = {
            "jql": jql,
            "maxResults": 5,
            "fields": ["summary", "status", "priority", "created", "labels"],
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(
                    f"{self._base}/rest/api/3/search/jql",
                    json=payload,
                    headers=self._headers,
                )
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            logger.warning("jira_client.search_failed service=%s error=%s", service, e)
            return []

        results = []
        for issue in data.get("issues") or []:
            fields = issue.get("fields") or {}
            st = fields.get("status") or {}
            cat_key = (st.get("statusCategory") or {}).get("key", "new")
            priority = (fields.get("priority") or {}).get("name", "")
            results.append({
                "key": issue.get("key", ""),
                "summary": fields.get("summary", ""),
                "status": _jira_status(cat_key),
                "status_name": st.get("name", ""),
                "priority": priority,
                "url": f"{self._base}/browse/{issue.get('key', '')}",
                "created": fields.get("created", ""),
            })

        return results


def build_jira_context(issues: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Сворачивает список тикетов в summary для FixAgent.

    Returns None если список пустой.
    """
    if not issues:
        return None

    open_issues = [i for i in issues if i["status"] == "open"]
    resolved_issues = [i for i in issues if i["status"] == "resolved"]

    return {
        "open": open_issues,
        "resolved": resolved_issues,
        "has_open": bool(open_issues),
        "has_resolved": bool(resolved_issues),
        "total": len(issues),
    }
