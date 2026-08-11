"""Тесты на gitlab_service: пагинация списка MR.

Находка второй волны кодревью: `mrs_merged_in_window` брала ОДНУ страницу
`per_page=25` с `order_by=updated_at` — а updated_at двигает любая активность
в посторонних MR (коммент, пайплайн). В активном backend-репо MR-ы, смёрженные
в целевое окно, выдавливались за границу первой страницы, и ответ «какой MR
вызвал проблему» был неполным, но уверенным.

Проверяем: обход страниц, ранний выход по updated_at, кап страниц с логом,
частичный результат при ошибке страницы и сортировку по merged_at DESC.
"""
import logging
from unittest.mock import patch

import httpx
import pytest

from app.services.gitlab_service import (
    _MR_MAX_PAGES,
    _MR_PAGE_SIZE,
    GitLabClient,
)

SINCE = "2026-08-10T10:00:00+00:00"
UNTIL = "2026-08-10T11:00:00+00:00"


def _mr(iid: int, merged_at: str, updated_at: str) -> dict:
    return {
        "iid": iid,
        "title": f"MR {iid}",
        "web_url": f"https://gitlab.example/mr/{iid}",
        "author": {"name": "Ярослав"},
        "merged_at": merged_at,
        "updated_at": updated_at,
    }


class _FakePages:
    """Мок httpx.AsyncClient: отдаёт заранее заготовленные страницы MR."""

    def __init__(self, pages: list, errors: dict | None = None):
        self.pages = pages
        self.errors = errors or {}
        self.requested_pages: list[int] = []

    def factory(self, *args, **kwargs):
        outer = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, headers=None, params=None):
                page = int((params or {}).get("page", 1))
                outer.requested_pages.append(page)
                if page in outer.errors:
                    raise outer.errors[page]
                data = outer.pages[page - 1] if page - 1 < len(outer.pages) else []
                return httpx.Response(200, json=data, request=httpx.Request("GET", url))

        return _Client()


async def _call(fake: _FakePages, since: str = SINCE, until: str = UNTIL):
    gl = GitLabClient("https://gitlab.example", "token")
    with patch("app.services.gitlab_service.httpx.AsyncClient", new=fake.factory):
        return await gl.mrs_merged_in_window(
            project_id=42, target_branch="prod", since=since, until=until
        )


@pytest.mark.asyncio
async def test_finds_mr_pushed_off_first_page_by_unrelated_activity():
    """Корень бага: первая страница забита MR-ами, которые лишь ОБНОВЛЯЛИСЬ
    недавно (комменты/пайплайны), а смёржены задолго до окна. Нужный MR — на
    второй странице."""
    noisy = [
        _mr(1000 + i, merged_at="2026-08-01T00:00:00+00:00",
            updated_at="2026-08-10T12:00:00+00:00")
        for i in range(_MR_PAGE_SIZE)
    ]
    wanted = _mr(77, merged_at="2026-08-10T10:30:00+00:00",
                 updated_at="2026-08-10T10:30:00+00:00")
    fake = _FakePages([noisy, [wanted]])

    out = await _call(fake)

    assert [mr["iid"] for mr in out] == [77]
    assert fake.requested_pages == [1, 2]


@pytest.mark.asyncio
async def test_stops_early_when_whole_page_is_older_than_since():
    """updated_at ≥ merged_at всегда: если вся полная страница обновлялась
    раньше `since`, ниже MR-ов из окна нет — лишние страницы не тянем."""
    stale = [
        _mr(2000 + i, merged_at="2026-07-01T00:00:00+00:00",
            updated_at="2026-07-02T00:00:00+00:00")
        for i in range(_MR_PAGE_SIZE)
    ]
    fake = _FakePages([stale, [_mr(1, "2026-08-10T10:30:00+00:00", "2026-08-10T10:30:00+00:00")]])

    out = await _call(fake)

    assert out == []
    assert fake.requested_pages == [1]


@pytest.mark.asyncio
async def test_single_short_page_does_not_request_more():
    """Обычный случай (репо не активный) — один запрос, как и раньше."""
    fake = _FakePages([[_mr(5, "2026-08-10T10:30:00+00:00", "2026-08-10T10:31:00+00:00")]])

    out = await _call(fake)

    assert [mr["iid"] for mr in out] == [5]
    assert fake.requested_pages == [1]


@pytest.mark.asyncio
async def test_page_cap_is_logged_not_silent(caplog):
    """Кап не должен молча обрезать окно: «MR не найден» ≠ «MR не было»."""
    full_page = [
        _mr(3000 + i, merged_at="2026-08-10T10:30:00+00:00",
            updated_at="2026-08-10T12:00:00+00:00")
        for i in range(_MR_PAGE_SIZE)
    ]
    fake = _FakePages([list(full_page) for _ in range(_MR_MAX_PAGES + 3)])

    with caplog.at_level(logging.WARNING, logger="app.services.gitlab_service"):
        out = await _call(fake)

    assert fake.requested_pages == list(range(1, _MR_MAX_PAGES + 1))
    assert len(out) == 5  # _MAX_MRS
    assert "page cap reached" in caplog.text
    assert f"pages={_MR_MAX_PAGES}" in caplog.text


@pytest.mark.asyncio
async def test_partial_result_when_later_page_fails():
    """Ошибка на 2-й странице не должна обнулять уже собранные MR."""
    page1 = [
        _mr(4000 + i, merged_at="2026-08-10T10:0%d:00+00:00" % (i % 10),
            updated_at="2026-08-10T12:00:00+00:00")
        for i in range(_MR_PAGE_SIZE)
    ]
    fake = _FakePages([page1], errors={2: httpx.ConnectError("gitlab unreachable")})

    out = await _call(fake)

    assert len(out) == 5
    assert fake.requested_pages == [1, 2]


@pytest.mark.asyncio
async def test_result_sorted_by_merged_at_desc_and_capped():
    """Из широкого скана нужны MR-ы, смёрженные ближе всего к инциденту."""
    page1 = [
        _mr(1, "2026-08-10T10:05:00+00:00", "2026-08-10T12:00:00+00:00"),
        _mr(2, "2026-08-10T10:55:00+00:00", "2026-08-10T12:00:00+00:00"),
        _mr(3, "2026-08-10T10:20:00+00:00", "2026-08-10T12:00:00+00:00"),
        _mr(4, "2026-08-10T10:45:00+00:00", "2026-08-10T12:00:00+00:00"),
        _mr(5, "2026-08-10T10:35:00+00:00", "2026-08-10T12:00:00+00:00"),
        _mr(6, "2026-08-10T10:10:00+00:00", "2026-08-10T12:00:00+00:00"),
        # вне окна — отфильтровывается
        _mr(7, "2026-08-09T10:10:00+00:00", "2026-08-10T12:00:00+00:00"),
    ]
    fake = _FakePages([page1])

    out = await _call(fake)

    assert [mr["iid"] for mr in out] == [2, 4, 5, 3, 6]


@pytest.mark.asyncio
async def test_first_page_error_returns_empty_list():
    fake = _FakePages([], errors={1: httpx.ConnectError("down")})
    assert await _call(fake) == []
    assert fake.requested_pages == [1]
