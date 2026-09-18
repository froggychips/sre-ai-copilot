"""`LogProvider`: интерфейс к логам и его первая реализация.

Главное, что здесь проверяется, — не перевод вызовов, а граница между
«данных нет» и «ошибок ноль». Если провайдер их склеит, молчание источника
станет неотличимо от здоровья сервиса, и это уедет в фундамент: поверх
провайдера строятся и наблюдения в графе, и диагностика.
"""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.context.seq_client import SeqQueryError
from app.providers.factory import make_log_provider
from app.providers.logs import LogProvider, Measurement, ServiceLogStats
from app.providers.seq_logs import SeqLogProvider

_SINCE = datetime(2026, 9, 17, 10, 0, 0)
_UNTIL = datetime(2026, 9, 17, 11, 0, 0)


def _provider() -> SeqLogProvider:
    return SeqLogProvider(name="prod", base_url="https://host/seq")


def _event(msg: str = "boom", app: str = "GR.WO.Bot") -> dict:
    # Плоский формат Properties — именно так выглядят живые WO-события
    # (recon 05.06.2026): сервис-тег лежит в `App`, а не в `Application`.
    return {"Id": "e1", "Level": "Error", "MessageTemplate": msg, "App": app}


# --- Measurement ----------------------------------------------------------

def test_unknown_is_not_zero():
    """None и 0 — разные ответы, и тип обязан их различать."""
    unknown = Measurement.unknown("источник молчит")
    zero = Measurement.of(0)

    assert not unknown.measured
    assert unknown.value is None
    assert zero.measured
    assert zero.value == 0


def test_or_else_substitutes_only_for_unknown():
    assert Measurement.unknown("нет").or_else(42) == 42
    assert Measurement.of(0).or_else(42) == 0


# --- count_events ---------------------------------------------------------

@pytest.mark.asyncio
async def test_count_reports_measured_zero():
    """Источник ответил «событий нет» — это измерение, а не незнание."""
    with patch.object(
        _provider()._client.__class__, "count_events_detailed",
        new=AsyncMock(return_value=(0, False)),
    ):
        result = await _provider().count_events("Error", _SINCE, _UNTIL)

    assert result.measured
    assert result.value == 0
    assert result.exact


@pytest.mark.asyncio
async def test_count_reports_unknown_when_source_fails():
    """Отказ источника НЕ должен выглядеть как ноль.

    Ровно этот случай стоил 12,8 часов слепоты 20.08.2026: NetworkPolicy
    перекрыла Seq, а синк отчитывался `rows=0`.
    """
    with patch.object(
        _provider()._client.__class__, "count_events_detailed",
        new=AsyncMock(side_effect=SeqQueryError("connection refused")),
    ):
        result = await _provider().count_events("Error", _SINCE, _UNTIL)

    assert not result.measured
    assert result.value is None
    assert "seq_unavailable" in result.reason


@pytest.mark.asyncio
async def test_capped_count_is_marked_inexact():
    """Упор в потолок пагинации — нижняя оценка, а не точное число.

    «Ровно 20000» и «не меньше 20000» — разные утверждения там, где по
    счёту принимают решение.
    """
    with patch.object(
        _provider()._client.__class__, "count_events_detailed",
        new=AsyncMock(return_value=(20000, True)),
    ):
        result = await _provider().count_events("Error", _SINCE, _UNTIL)

    assert result.measured
    assert result.value == 20000
    assert not result.exact


# --- service_stats --------------------------------------------------------

@pytest.mark.asyncio
async def test_service_stats_aggregates_events():
    events = [_event("boom"), _event("boom"), _event("crash", app="GR.WO.Push")]
    with patch.object(
        _provider()._client.__class__, "top_messages",
        new=AsyncMock(return_value=events),
    ):
        result = await _provider().service_stats("Error", _SINCE, _UNTIL)

    assert result.measured
    stats = result.value
    assert stats["GR.WO.Bot"].count == 2
    assert stats["GR.WO.Bot"].top_message == "boom"
    assert stats["GR.WO.Push"].count == 1


@pytest.mark.asyncio
async def test_empty_window_is_measured_silence():
    """Источник ответил пустотой — это тишина, а не незнание."""
    with patch.object(
        _provider()._client.__class__, "top_messages",
        new=AsyncMock(return_value=[]),
    ):
        result = await _provider().service_stats("Error", _SINCE, _UNTIL)

    assert result.measured
    assert result.value == {}


@pytest.mark.asyncio
async def test_failed_window_is_unknown_not_empty():
    """Отказ — не пустой словарь: иначе синк запишет тишину вместо слепоты."""
    with patch.object(
        _provider()._client.__class__, "top_messages",
        new=AsyncMock(side_effect=SeqQueryError("timeout")),
    ):
        result = await _provider().service_stats("Error", _SINCE, _UNTIL)

    assert not result.measured
    assert result.value is None


@pytest.mark.asyncio
async def test_full_page_marks_stats_inexact():
    """Набрали ровно limit — были ли ещё события, неизвестно."""
    events = [_event(f"m{i}") for i in range(5)]
    with patch.object(
        _provider()._client.__class__, "top_messages",
        new=AsyncMock(return_value=events),
    ):
        result = await _provider().service_stats("Error", _SINCE, _UNTIL, limit=5)

    assert result.measured
    assert not result.exact


# --- фабрика --------------------------------------------------------------

def test_factory_builds_seq_by_default():
    provider = make_log_provider(name="prod", url="https://host/seq", token="t")
    assert isinstance(provider, SeqLogProvider)
    assert isinstance(provider, LogProvider)
    assert provider.name == "prod"


def test_unknown_backend_is_an_error_not_a_silent_fallback():
    """Опечатка в настройке не должна выглядеть как рабочая система."""
    with pytest.raises(ValueError, match="clickhouse"):
        make_log_provider(name="x", url="u", backend="clickhouse")


def test_interface_does_not_depend_on_any_implementation():
    """Модуль интерфейса не должен знать про источники.

    Проверяется по импортам, а не по тексту: докстринги как раз обязаны
    объяснять, откуда взялось требование, и упоминают там и Seq, и
    ClickHouse. Зависимость создаёт import, а не абзац.

    Если интерфейс потянет за собой seq_client, реализация поверх
    ClickHouse унаследует его форму — абстракция протечёт на второй же
    реализации, то есть ровно там, где её впервые проверят.
    """
    import ast
    import inspect

    from app.providers import logs

    tree = ast.parse(inspect.getsource(logs))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)

    # `app.providers.measurement` — общий словарь измеримости, а не
    # источник: один вопрос «данных нет или ноль» и один тип-ответ на него
    # для логов и метрик. Запрещены зависимости от РЕАЛИЗАЦИЙ.
    allowed = {"app.providers.measurement"}
    leaked = [m for m in imported if m.startswith("app.") and m not in allowed]
    assert not leaked, f"интерфейс зависит от реализации: {leaked}"


def test_service_stats_type_keeps_unattributed_events():
    """События без имени приложения — законный случай, не ошибка.

    Они попадают в KG со `service_id=NULL`, и терять их нельзя.
    """
    stats = ServiceLogStats(service=None, count=3, top_message="boom")
    assert stats.service is None
    assert stats.count == 3


# --- слепота источника не должна выглядеть успехом ------------------------

@pytest.mark.asyncio
async def test_fully_unmeasured_instance_counts_as_failure(monkeypatch):
    """Инстанс, не ответивший ни по одному уровню, — это провал, не тишина.

    Защита «reached == 0 → error» появилась после 20.08.2026, когда
    NetworkPolicy перекрыла все инстансы, задача завершалась SUCCESS с
    rows=0 и 12,8 часа никто не знал о слепоте. Но она полагалась на то,
    что недоступность долетит исключением, а `_sync_instance` гасил отказы
    внутри и всё равно попадал в `reached`.
    """
    from app.knowledge_graph import seq_logs_sync as sync
    from app.providers.logs import Measurement

    class _DeadProvider:
        name = "prod"

        async def service_stats(self, **_kw):
            return Measurement.unknown("seq_unavailable: connection refused")

    monkeypatch.setattr(sync, "make_log_provider", lambda **_kw: _DeadProvider())

    with pytest.raises(sync.LogSourceUnavailable):
        await sync._sync_instance(
            db=MagicMock(),
            instance={"name": "prod", "url": "https://host/seq"},
            since=_SINCE, until=_UNTIL, ts_bucket=_SINCE,
        )


@pytest.mark.asyncio
async def test_partially_measured_instance_still_reports(monkeypatch):
    """Ответил хотя бы один уровень — данные есть, терять их незачем."""
    from app.knowledge_graph import seq_logs_sync as sync
    from app.providers.logs import Measurement

    class _FlakyProvider:
        name = "prod"

        def __init__(self):
            self.calls = 0

        async def service_stats(self, **_kw):
            self.calls += 1
            if self.calls == 1:
                return Measurement.of({})
            return Measurement.unknown("seq_unavailable: timeout")

    monkeypatch.setattr(sync, "make_log_provider", lambda **_kw: _FlakyProvider())

    stats = await sync._sync_instance(
        db=MagicMock(),
        instance={"name": "prod", "url": "https://host/seq"},
        since=_SINCE, until=_UNTIL, ts_bucket=_SINCE,
    )

    assert stats["measured"] == 1
    assert stats["unmeasured"] == 2, "непрослушанные окна должны быть посчитаны"


@pytest.mark.asyncio
async def test_all_instances_blind_marks_run_failed(monkeypatch):
    """Все инстансы слепы → прогон возвращает error, heartbeat не пишется."""
    from app.knowledge_graph import seq_logs_sync as sync
    from app.providers.logs import Measurement

    class _DeadProvider:
        name = "prod"

        async def service_stats(self, **_kw):
            return Measurement.unknown("seq_unavailable")

    monkeypatch.setattr(sync, "make_log_provider", lambda **_kw: _DeadProvider())
    monkeypatch.setattr(sync, "_load_instances", lambda: [
        {"name": "prod", "url": "https://a/seq"},
        {"name": "preprod", "url": "https://b/seq"},
    ])

    result = await sync._sync_seq_logs_async(db=MagicMock(), window_minutes=10)

    assert result["reached"] == 0
    assert result["failed"] == 2
    assert "error" in result, "слепой прогон не должен выглядеть успешным"
