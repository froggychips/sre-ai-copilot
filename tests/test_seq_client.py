"""Тесты на SeqClient — упор на count_events (diag H4).

Живого Seq нет → мокаем httpx.AsyncClient. Seq `/api/events` отдаёт СТРАНИЦУ
raw-событий (list), без конверта `{Total}`; реальный total count_events
получает пагинацией по курсору `afterId` и суммированием страниц. Тесты
проверяют: (1) multi-page суммирование → N (не 1), (2) пустой результат → 0,
(3) поведение на cap (WARNING + floor-оценка, без тихой обрезки).
"""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.context.seq_client import SeqClient


def _make_event(i: int) -> dict:
    """Минимальное Seq-событие с уникальным Id (для курсора afterId)."""
    return {"Id": f"event-{i}", "Level": "Error", "MessageTemplate": "boom"}


def _paged_client(pages):
    """Собирает mock httpx.AsyncClient, отдающий заранее заготовленные страницы.

    `pages` — список list[event]; каждый последовательный GET возвращает
    следующую страницу. Возвращает (mock_client_cls, calls) где calls копит
    params каждого запроса для ассертов на курсор.
    """
    calls = []
    page_iter = iter(pages)

    async def fake_get(url, params=None, headers=None):
        calls.append(params or {})
        try:
            page = next(page_iter)
        except StopIteration:
            page = []
        resp = MagicMock()
        resp.json.return_value = page
        resp.raise_for_status = MagicMock()
        return resp

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=fake_get)

    mock_cls = MagicMock(return_value=mock_client)
    return mock_cls, calls


_SINCE = datetime(2026, 6, 25, 10, 0, 0)
_UNTIL = datetime(2026, 6, 25, 11, 0, 0)


# ---------- count_events: multi-page суммирование -------------------------

@pytest.mark.asyncio
async def test_count_events_sums_across_pages():
    """N событий на нескольких страницах → count_events возвращает N, не 1."""
    page_size = SeqClient._COUNT_PAGE_SIZE
    # Полная страница + хвост (короче page_size) ⇒ ровно 2 запроса.
    full = [_make_event(i) for i in range(page_size)]
    tail = [_make_event(page_size + i) for i in range(7)]
    mock_cls, calls = _paged_client([full, tail])

    with patch("httpx.AsyncClient", mock_cls):
        client = SeqClient(base_url="https://host/seq")
        total = await client.count_events("Error", _SINCE, _UNTIL)

    assert total == page_size + 7
    assert total != 1  # явная регрессия-страховка против старого бага
    # Вторая страница должна нести курсор afterId = Id последнего из первой.
    assert len(calls) == 2
    assert "afterId" not in calls[0]
    assert calls[1]["afterId"] == f"event-{page_size - 1}"


@pytest.mark.asyncio
async def test_count_events_single_short_page():
    """Одна короткая страница (< page_size) → реальное число, без 2-го запроса."""
    mock_cls, calls = _paged_client([[_make_event(i) for i in range(5)]])

    with patch("httpx.AsyncClient", mock_cls):
        client = SeqClient(base_url="https://host/seq")
        total = await client.count_events("Error", _SINCE, _UNTIL)

    assert total == 5
    assert len(calls) == 1  # короткая страница = хвост, перелистывать нечего


# ---------- count_events: пустой результат --------------------------------

@pytest.mark.asyncio
async def test_count_events_empty_returns_zero():
    """Нет событий → 0."""
    mock_cls, _ = _paged_client([[]])

    with patch("httpx.AsyncClient", mock_cls):
        client = SeqClient(base_url="https://host/seq")
        total = await client.count_events("Fatal", _SINCE, _UNTIL)

    assert total == 0


@pytest.mark.asyncio
async def test_count_events_http_error_returns_zero():
    """Любая ошибка HTTP → graceful 0 (не падаем)."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=Exception("connection refused"))
    mock_cls = MagicMock(return_value=mock_client)

    with patch("httpx.AsyncClient", mock_cls):
        client = SeqClient(base_url="https://host/seq")
        total = await client.count_events("Error", _SINCE, _UNTIL)

    assert total == 0


# ---------- count_events: поведение на cap --------------------------------

@pytest.mark.asyncio
async def test_count_events_cap_hit_warns_and_returns_floor(caplog):
    """Все страницы полные (упёрлись в _COUNT_MAX_PAGES) → WARNING + floor-оценка.

    Проверяем что НЕ обрезаем молча: возвращается max_pages*page_size и в логах
    есть предупреждение о том, что реальный объём больше.
    """
    page_size = SeqClient._COUNT_PAGE_SIZE
    max_pages = SeqClient._COUNT_MAX_PAGES
    # Бесконечный источник полных страниц — пусть отдаёт больше, чем cap.
    full_pages = [
        [_make_event(p * page_size + i) for i in range(page_size)]
        for p in range(max_pages + 5)
    ]
    mock_cls, calls = _paged_client(full_pages)

    import logging
    with patch("httpx.AsyncClient", mock_cls), caplog.at_level(logging.WARNING):
        client = SeqClient(base_url="https://host/seq")
        total = await client.count_events("Error", _SINCE, _UNTIL)

    # Уперлись в потолок: ровно max_pages запросов, floor-оценка.
    assert len(calls) == max_pages
    assert total == max_pages * page_size
    assert any("count_cap_hit" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_count_events_event_without_id_breaks_pagination(caplog):
    """Полная страница без Id у последнего события → не зацикливаемся, WARNING."""
    page_size = SeqClient._COUNT_PAGE_SIZE
    # Полная страница, но у событий нет Id → курсор не построить.
    page = [{"Level": "Error", "MessageTemplate": "no-id"} for _ in range(page_size)]
    mock_cls, calls = _paged_client([page])

    import logging
    with patch("httpx.AsyncClient", mock_cls), caplog.at_level(logging.WARNING):
        client = SeqClient(base_url="https://host/seq")
        total = await client.count_events("Error", _SINCE, _UNTIL)

    assert total == page_size  # что насчитали, то и вернули
    assert len(calls) == 1     # без курсора дальше не идём
    assert any("count_no_cursor" in rec.message for rec in caplog.records)


# ---------- count_events: {"Events": [...]} конверт-формат -----------------

@pytest.mark.asyncio
async def test_count_events_handles_events_envelope():
    """Если инстанс отдаёт {"Events": [...]} — тоже считаем корректно."""
    calls = []

    async def fake_get(url, params=None, headers=None):
        calls.append(params or {})
        resp = MagicMock()
        resp.json.return_value = {"Events": [_make_event(i) for i in range(3)]}
        resp.raise_for_status = MagicMock()
        return resp

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=fake_get)
    mock_cls = MagicMock(return_value=mock_client)

    with patch("httpx.AsyncClient", mock_cls):
        client = SeqClient(base_url="https://host/seq")
        total = await client.count_events("Error", _SINCE, _UNTIL)

    assert total == 3
    assert len(calls) == 1  # 3 < page_size ⇒ хвост


# ---------- отказ Seq не должен выглядеть как тишина ----------------------
#
# Прецедент 20.08.2026: NetworkPolicy перекрыла доступ ко всем восьми
# инстансам Seq. `top_messages` возвращала пустой список и писала причину в
# debug, синк отчитывался `rows=0`, и 12,8 часа никто не знал, что логи вне
# обзора — отставание доросло до 751 минуты. Пустота это факт, отказ —
# отсутствие факта, и смешивать их нельзя.


@pytest.mark.asyncio
async def test_top_messages_raises_on_transport_error():
    """Сетевой отказ — исключение, а не пустой список."""
    import httpx

    from app.context.seq_client import SeqQueryError

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectTimeout("timed out"))

    with patch("httpx.AsyncClient", MagicMock(return_value=mock_client)):
        c = SeqClient(base_url="https://seq.example", api_key="k")
        with pytest.raises(SeqQueryError) as exc:
            await c.top_messages(level="Error", since=_SINCE, until=_UNTIL)
    assert "ConnectTimeout" in str(exc.value)


@pytest.mark.asyncio
async def test_top_messages_raises_on_http_error():
    """401/403/500 тоже отказ: «токен не тот» неотличимо от «ошибок нет»."""
    import httpx

    from app.context.seq_client import SeqQueryError

    resp = MagicMock()
    resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("401", request=MagicMock(), response=MagicMock())
    )
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=resp)

    with patch("httpx.AsyncClient", MagicMock(return_value=mock_client)):
        c = SeqClient(base_url="https://seq.example", api_key="k")
        with pytest.raises(SeqQueryError):
            await c.top_messages(level="Error", since=_SINCE, until=_UNTIL)


@pytest.mark.asyncio
async def test_top_messages_empty_result_is_not_an_error():
    """А вот честно пустое окно — просто пустой список, без исключения."""
    mock_cls, _ = _paged_client([[]])
    with patch("httpx.AsyncClient", mock_cls):
        c = SeqClient(base_url="https://seq.example", api_key="k")
        events = await c.top_messages(level="Error", since=_SINCE, until=_UNTIL)
    assert events == []
