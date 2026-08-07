"""Тесты на JiraClient и build_jira_context."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.context.jira_client import (
    JiraClient,
    _jira_status,
    _jql_quote,
    build_jira_context,
)


# ---------- _jira_status --------------------------------------------------

def test_jira_status_done():
    assert _jira_status("done") == "resolved"


def test_jira_status_new():
    assert _jira_status("new") == "open"


def test_jira_status_indeterminate():
    assert _jira_status("indeterminate") == "open"


def test_jira_status_unknown_defaults_open():
    assert _jira_status("unknown_future_status") == "open"


# ---------- build_jira_context -------------------------------------------

def test_build_jira_context_empty():
    assert build_jira_context([]) is None


def test_build_jira_context_open_issues():
    ctx = build_jira_context([
        {"key": "WO-100", "summary": "notificator crashes", "status": "open",
         "status_name": "В работе", "priority": "Highest", "url": "https://x/WO-100", "created": "2026-05-01"},
    ])
    assert ctx is not None
    assert ctx["has_open"] is True
    assert ctx["has_resolved"] is False
    assert len(ctx["open"]) == 1
    assert ctx["open"][0]["key"] == "WO-100"


def test_build_jira_context_resolved_only():
    ctx = build_jira_context([
        {"key": "WO-99", "summary": "old crash fixed", "status": "resolved",
         "status_name": "Готово", "priority": "Medium", "url": "https://x/WO-99", "created": "2026-04-01"},
    ])
    assert ctx["has_open"] is False
    assert ctx["has_resolved"] is True
    assert len(ctx["resolved"]) == 1


def test_build_jira_context_mixed():
    issues = [
        {"key": "WO-1", "summary": "open bug", "status": "open",
         "status_name": "К выполнению", "priority": "High", "url": "u1", "created": ""},
        {"key": "WO-2", "summary": "fixed bug", "status": "resolved",
         "status_name": "Готово", "priority": "Low", "url": "u2", "created": ""},
    ]
    ctx = build_jira_context(issues)
    assert ctx["has_open"] is True
    assert ctx["has_resolved"] is True
    assert ctx["total"] == 2


# ---------- _jql_quote (JQL-инъекция через alert-label) ------------------

def test_jql_quote_plain_value_untouched():
    assert _jql_quote("town-service") == "town-service"


def test_jql_quote_escapes_double_quotes():
    assert _jql_quote('x" OR project = "FINANCE') == 'x\\" OR project = \\"FINANCE'


def test_jql_quote_escapes_backslash_before_quote():
    # Бэкслеш экранируется ПЕРВЫМ, иначе `\"` в инпуте разэкранировал бы кавычку.
    assert _jql_quote('a\\" OR x') == 'a\\\\\\" OR x'


def test_jql_quote_strips_control_chars():
    assert _jql_quote("svc\n\x00\tname") == "svcname"


def test_jql_quote_empty_and_none():
    assert _jql_quote("") == ""
    assert _jql_quote(None) == ""  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_search_by_service_escapes_injection_in_jql():
    """Label `service` с кавычкой не разрывает конъюнкцию project/labels."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"issues": []}
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        client = JiraClient(
            base_url="https://juicybuttons.atlassian.net",
            email="test@test.com",
            api_token="token123",
        )
        await client.search_by_service('x" OR project = "FINANCE')

    jql = mock_client.post.await_args.kwargs["json"]["jql"]
    # Внутри строки все кавычки инъекции экранированы…
    assert 'summary ~ "x\\" OR project = \\"FINANCE"' in jql
    # …и неэкранированного разрыва `..." OR project = "...` в JQL нет.
    assert '~ "x" OR' not in jql


def test_search_by_service_sync_escapes_injection_in_jql():
    """Sync-вариант обязан использовать тот же хелпер (одна точка экранирования)."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"issues": []}
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post = MagicMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        client = JiraClient(
            base_url="https://juicybuttons.atlassian.net",
            email="test@test.com",
            api_token="token123",
        )
        client.search_by_service_sync('y" OR labels = "prod-secrets')

    jql = mock_client.post.call_args.kwargs["json"]["jql"]
    assert 'summary ~ "y\\" OR labels = \\"prod-secrets"' in jql
    assert '~ "y" OR' not in jql


# ---------- JiraClient.search_by_service (mocked HTTP) -------------------

@pytest.mark.asyncio
async def test_jira_client_returns_parsed_issues():
    fake_response = {
        "issues": [
            {
                "key": "WO-500",
                "fields": {
                    "summary": "notificator SIGSEGV crash",
                    "status": {
                        "name": "В работе",
                        "statusCategory": {"key": "indeterminate"},
                    },
                    "priority": {"name": "Highest"},
                    "created": "2026-05-10T10:00:00.000+0000",
                    "labels": ["backend"],
                },
            }
        ]
    }

    mock_resp = MagicMock()
    mock_resp.json.return_value = fake_response
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        client = JiraClient(
            base_url="https://juicybuttons.atlassian.net",
            email="test@test.com",
            api_token="token123",
        )
        results = await client.search_by_service("notificator", "squad-10-shared")

    assert len(results) == 1
    assert results[0]["key"] == "WO-500"
    assert results[0]["status"] == "open"
    assert results[0]["priority"] == "Highest"
    assert "juicybuttons.atlassian.net/browse/WO-500" in results[0]["url"]


@pytest.mark.asyncio
async def test_jira_client_returns_empty_on_http_error():
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=Exception("connection refused"))
        mock_client_cls.return_value = mock_client

        client = JiraClient(
            base_url="https://juicybuttons.atlassian.net",
            email="test@test.com",
            api_token="token",
        )
        results = await client.search_by_service("notificator")

    assert results == []
