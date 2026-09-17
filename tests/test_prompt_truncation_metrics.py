"""Обрезка входа промпта должна быть ВИДНА, а не только записана в лог.

Роадмап предлагает строить EvidenceBundle с приоритетами и token budget.
Это оправдано ровно настолько, насколько обрезка реально срабатывает — а
до 17.09.2026 факт обрезки жил только в structlog-записи, и чтобы узнать,
теряем ли мы evidence, нужно было идти в логи и надеяться, что нужный под
ещё жив. Счётчик отвечает на это фактом.
"""
import pytest

from app.observability.ai_metrics import (PROMPT_INPUT_CHARS,
                                          PROMPT_INPUT_TRUNCATED)
from app.services.prompt_guard import prompt_guard


def _truncation_count() -> float:
    return PROMPT_INPUT_TRUNCATED._value.get()


def _observed_count() -> float:
    return PROMPT_INPUT_CHARS._sum.get()


def test_truncation_is_counted(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "PROMPT_INPUT_MAX_CHARS", 100, raising=False)
    before = _truncation_count()

    result = prompt_guard.sanitize("x" * 500)

    assert _truncation_count() == before + 1, "обрезка обязана попасть в счётчик"
    assert "truncated" in result, "маркер обрезки должен остаться в тексте"


def test_short_input_is_not_counted(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "PROMPT_INPUT_MAX_CHARS", 10_000, raising=False)
    before = _truncation_count()

    prompt_guard.sanitize("короткий ввод")

    assert _truncation_count() == before, "без обрезки счётчик не растёт"


def test_input_size_is_observed_even_without_truncation(monkeypatch):
    """Размер пишется ВСЕГДА — иначе не видно, близко ли мы к пределу.

    Счётчик обрезок отвечает «режем или нет», гистограмма — «насколько не
    влезаем». Без второй цифры непонятно, что делать: резать умнее или
    поднять лимит.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "PROMPT_INPUT_MAX_CHARS", 10_000, raising=False)
    before = _observed_count()

    prompt_guard.sanitize("y" * 300)

    assert _observed_count() > before, "размер входа должен наблюдаться всегда"


def test_metrics_failure_does_not_break_sanitize(monkeypatch):
    """Телеметрия не важнее вызова модели: её сбой не ломает запрос.

    Падать заставляем САМУ метрику и держим подмену активной во время
    sanitize. Если подменить хелперы на no-op перед вызовом, тест пройдёт
    и без try/except внутри них — то есть не докажет ничего.
    """
    import app.observability.ai_metrics as ai_metrics
    from app.config import settings

    class _BrokenMetric:
        def observe(self, *_a, **_k):
            raise RuntimeError("метрики недоступны")

        def inc(self, *_a, **_k):
            raise RuntimeError("метрики недоступны")

    broken = _BrokenMetric()
    monkeypatch.setattr(ai_metrics, "PROMPT_INPUT_CHARS", broken)
    monkeypatch.setattr(ai_metrics, "PROMPT_INPUT_TRUNCATED", broken)
    monkeypatch.setattr(settings, "PROMPT_INPUT_MAX_CHARS", 50, raising=False)

    # Контроль подмены: метрика действительно падает прямо сейчас.
    with pytest.raises(RuntimeError):
        ai_metrics.PROMPT_INPUT_CHARS.observe(1)
    with pytest.raises(RuntimeError):
        ai_metrics.PROMPT_INPUT_TRUNCATED.inc()

    # Обе метрики на пути обрезки — и observe, и inc; sanitize обязан
    # отработать при живой подмене.
    result = prompt_guard.sanitize("z" * 200)

    assert "truncated" in result, "обрезка должна произойти несмотря на сбой метрик"
