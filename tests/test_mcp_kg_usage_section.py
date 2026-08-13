"""Секция дайджеста «KG через MCP»: считаем людей, а не ботов.

Метрика отвечает на вопрос «граф вообще кому-то нужен». Технические счётчики
(узлы, рёбра) растут сами и об этом молчат; обращения людей — не растут.

Ключевая деталь — фильтрация служебных ключей. Замер на проде 08.08.2026 за
неделю: `knowledge-generator` сделал 111 588 вызовов против ~1 500 у всех 47
человек вместе. Без фильтра секция показывала бы активность бота и не менялась
бы от того, пользуются ли инструментами люди.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.stats_digest import _is_human_key, mcp_kg_usage_section
from app.services.digest import failures as digest_failures


@pytest.mark.parametrize("key,expected", [
    # люди — key_name это «Имя Фамилия»
    ("Виталий Шабалин", True),
    ("Захар Бушуев", True),
    # известные служебные ключи
    ("knowledge-generator", False),
    ("squad-medic-robot", False),
    ("discord-bot", False),
    ("mcp-preplanner", False),
    ("claude-ybobryashov-rot", False),
    # будущие боты по общему шаблону — страховка, а не догадка
    ("release-bot", False),
    ("some-robot", False),
    ("mcp-newthing", False),
    # пустой ключ человеком не считается
    ("", False),
])
def test_is_human_key(key, expected):
    assert _is_human_key(key) is expected


def _vm(today: dict, week: dict):
    vm = AsyncMock()

    async def by_labels(query, labels):
        return week if "[7d]" in query else today

    vm.query_instant_by_labels = AsyncMock(side_effect=by_labels)
    return vm


@pytest.mark.asyncio
async def test_bot_traffic_does_not_leak_into_human_count():
    """Регрессия: бот с сотней тысяч вызовов не должен попадать в цифру."""
    today = {
        ("kg_service", "knowledge-generator"): 111588.0,
        ("kg_service", "Виталий Шабалин"): 12.0,
        ("kg_timeline", "Захар Бушуев"): 8.0,
    }
    text = await mcp_kg_usage_section(_vm(today, {}))

    assert "20 обращений от 2 чел." in text, text
    assert "111588" not in text and "111 588" not in text


@pytest.mark.asyncio
async def test_week_window_gives_context():
    """Недельное окно, а не «вчера»: обращения людей подчиняются рабочему циклу.

    Сравнение субботы с пятницей всегда даёт «падение», понедельника с
    воскресеньем — «рост». Замер 08.08.2026 (суббота): 3 активных человека
    против 13 в пятницу — секция кричала бы о проблеме каждые выходные.
    """
    today = {("kg_service", "Иван Иванов"): 30.0}
    week = {
        ("kg_service", "Иван Иванов"): 140.0,
        ("kg_timeline", "Пётр Петров"): 70.0,
    }

    text = await mcp_kg_usage_section(_vm(today, week))

    assert "сегодня: 30 обращений от 1 чел." in text
    assert "за 7 дней: 210 от 2 чел." in text
    assert "в среднем 30/день" in text


@pytest.mark.asyncio
async def test_top_tools_listed():
    today = {
        ("kg_service", "A A"): 10.0,
        ("kg_timeline", "B B"): 25.0,
        ("kg_query", "A A"): 5.0,
        ("kg_alerts", "B B"): 1.0,
    }
    text = await mcp_kg_usage_section(_vm(today, {}))

    # топ-3 по убыванию, четвёртый не показан
    assert "`kg_timeline` 25" in text
    assert "`kg_service` 10" in text
    assert "`kg_query` 5" in text
    assert "kg_alerts" not in text


@pytest.mark.asyncio
async def test_silent_when_no_metric_at_all():
    """Нет метрик — молчим, а не рисуем «0 обращений».

    Ноль здесь неотличим от «tools-server не скрейпится», а канон проекта —
    не выдавать отсутствие данных за факт.
    """
    assert await mcp_kg_usage_section(_vm({}, {})) == ""


@pytest.mark.asyncio
async def test_only_bots_today_reads_as_no_human_usage():
    """Если ходили только боты — людской счёт ноль, секция молчит."""
    today = {("kg_service", "knowledge-generator"): 5000.0}
    assert await mcp_kg_usage_section(_vm(today, {})) == ""


@pytest.mark.asyncio
async def test_vm_failure_is_reported_not_swallowed():
    """Сбой VM помечает секцию упавшей — иначе дыра в дайджесте незаметна."""
    from app.services import stats_digest as sd

    vm = AsyncMock()
    vm.query_instant_by_labels = AsyncMock(side_effect=RuntimeError("vm down"))

    sd._reset_section_failures()
    assert await mcp_kg_usage_section(vm) == ""
    assert "mcp_kg_usage_section" in digest_failures.failed_sections()


@pytest.mark.asyncio
async def test_quiet_day_is_not_reported_as_problem():
    """Тихий день сам по себе не сигнал — секция не делает выводов за читателя.

    Выходной с тремя обращениями выглядит одинаково с поломкой; отличить их
    по одной цифре нельзя, поэтому даём недельный контекст и молчим об оценках.
    """
    today = {("kg_service", "A A"): 3.0}
    week = {("kg_service", "A A"): 200.0, ("kg_service", "B B"): 150.0}

    text = await mcp_kg_usage_section(_vm(today, week))

    assert "сегодня: 3 обращений от 1 чел." in text
    assert "за 7 дней: 350 от 2 чел." in text
    for alarm in ("меньше", "падение", "↓"):
        assert alarm not in text
