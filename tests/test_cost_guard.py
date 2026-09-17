"""Потолок расхода на LLM — предохранитель между сервисом и деньгами.

Проверяется не только «считает ли он сумму», но и как он ведёт себя, когда
не знает ответа: для предохранителя это и есть главный вопрос. Незнание
здесь трактуется как запрет, и именно это отличает его от телеметрии
рядом, которая намеренно fail-open.
"""
import datetime as dt

import pytest

from app.config import Settings, settings
from app.services import cost_guard


class _FakeRedis:
    """Минимальный Redis: только то, что использует cost_guard."""

    def __init__(self, store=None, fail=False):
        self.store = dict(store or {})
        self.fail = fail
        self.expires = {}

    def get(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        return self.store.get(key)

    def pipeline(self):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.ops = []

    def incrby(self, key, amount):
        self.ops.append(("incrby", key, amount))
        return self

    def expire(self, key, ttl):
        self.ops.append(("expire", key, ttl))
        return self

    def execute(self):
        if self.redis.fail:
            raise ConnectionError("redis down")
        for op in self.ops:
            if op[0] == "incrby":
                cur = int(self.redis.store.get(op[1], 0))
                self.redis.store[op[1]] = cur + op[2]
            else:
                self.redis.expires[op[1]] = op[2]
        return [None] * len(self.ops)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(cost_guard, "_redis", lambda: fake)
    return fake


@pytest.fixture
def broken_redis(monkeypatch):
    fake = _FakeRedis(fail=True)
    monkeypatch.setattr(cost_guard, "_redis", lambda: fake)
    return fake


# --- цена вызова ----------------------------------------------------------

def test_known_model_priced_from_table(monkeypatch):
    monkeypatch.setattr(
        settings, "LLM_PRICE_PER_MTOK",
        {"m": {"input": 3.0, "output": 15.0}}, raising=False,
    )
    # 1M input по $3 + 1M output по $15.
    assert cost_guard.estimate_cost_usd("m", 1_000_000, 1_000_000) == pytest.approx(18.0)


def test_unknown_model_uses_expensive_fallback(monkeypatch):
    """Неизвестная модель считается дорого — и это не перестраховка.

    Недооценка пропускает трату мимо потолка, то есть отключает
    предохранитель ровно тогда, когда о модели ничего не известно.
    """
    monkeypatch.setattr(settings, "LLM_PRICE_PER_MTOK", {"known": {"input": 1.0, "output": 1.0}}, raising=False)
    monkeypatch.setattr(settings, "LLM_PRICE_FALLBACK_INPUT", 15.0, raising=False)
    monkeypatch.setattr(settings, "LLM_PRICE_FALLBACK_OUTPUT", 75.0, raising=False)

    known = cost_guard.estimate_cost_usd("known", 1_000_000, 1_000_000)
    unknown = cost_guard.estimate_cost_usd("нет-такой-модели", 1_000_000, 1_000_000)

    assert unknown > known, "неизвестная модель не должна стоить дешевле известной"
    assert unknown == pytest.approx(90.0)


def test_negative_usage_never_refunds(monkeypatch):
    """Битый usage не должен «возвращать» деньги в бюджет."""
    monkeypatch.setattr(settings, "LLM_PRICE_PER_MTOK", {"m": {"input": 3.0, "output": 15.0}}, raising=False)
    assert cost_guard.estimate_cost_usd("m", -1_000_000, -1_000_000) == 0.0


# --- учёт расхода ---------------------------------------------------------

def test_spend_accumulates_across_calls(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "LLM_PRICE_PER_MTOK", {"m": {"input": 3.0, "output": 15.0}}, raising=False)
    now = dt.datetime(2026, 9, 17, 12, 0, tzinfo=dt.timezone.utc)

    cost_guard.record_spend("m", 1_000_000, 0, now=now)
    cost_guard.record_spend("m", 1_000_000, 0, now=now)

    assert cost_guard.spent_today_usd(now) == pytest.approx(6.0)


def test_spend_is_scoped_to_utc_day(fake_redis, monkeypatch):
    """Счётчик суточный: завтрашний день начинается с нуля."""
    monkeypatch.setattr(settings, "LLM_PRICE_PER_MTOK", {"m": {"input": 3.0, "output": 0.0}}, raising=False)
    today = dt.datetime(2026, 9, 17, 23, 59, tzinfo=dt.timezone.utc)
    tomorrow = dt.datetime(2026, 9, 18, 0, 1, tzinfo=dt.timezone.utc)

    cost_guard.record_spend("m", 1_000_000, 0, now=today)

    assert cost_guard.spent_today_usd(today) == pytest.approx(3.0)
    assert cost_guard.spent_today_usd(tomorrow) == 0.0


def test_counter_key_gets_ttl(fake_redis, monkeypatch):
    """Без TTL ключи копились бы по одному на каждый прожитый день."""
    monkeypatch.setattr(settings, "LLM_PRICE_PER_MTOK", {"m": {"input": 3.0, "output": 0.0}}, raising=False)
    now = dt.datetime(2026, 9, 17, tzinfo=dt.timezone.utc)

    cost_guard.record_spend("m", 1_000_000, 0, now=now)

    assert fake_redis.expires[cost_guard._today_key(now)] == cost_guard._KEY_TTL_SECONDS


# --- вердикт --------------------------------------------------------------

def test_allows_while_under_limit(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", 10.0, raising=False)
    monkeypatch.setattr(settings, "LLM_PRICE_PER_MTOK", {"m": {"input": 3.0, "output": 0.0}}, raising=False)
    now = dt.datetime(2026, 9, 17, tzinfo=dt.timezone.utc)
    cost_guard.record_spend("m", 1_000_000, 0, now=now)

    verdict = cost_guard.check_budget(now)

    assert verdict.allowed
    assert verdict.spent_usd == pytest.approx(3.0)


def test_denies_when_limit_reached(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", 5.0, raising=False)
    monkeypatch.setattr(settings, "LLM_PRICE_PER_MTOK", {"m": {"input": 5.0, "output": 0.0}}, raising=False)
    now = dt.datetime(2026, 9, 17, tzinfo=dt.timezone.utc)
    cost_guard.record_spend("m", 1_000_000, 0, now=now)

    verdict = cost_guard.check_budget(now)

    assert not verdict.allowed
    assert verdict.reason == "daily_budget_exhausted"


def test_unknown_state_denies(broken_redis, monkeypatch):
    """Redis недоступен при ЗАДАННОМ потолке — отказ, а не «трать дальше».

    Это то самое место, где предохранитель обязан вести себя иначе, чем
    телеметрия: она fail-open, потому что метрика не важнее вызова модели;
    он fail-closed, потому что деньги — важнее.
    """
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", 10.0, raising=False)

    verdict = cost_guard.check_budget()

    assert not verdict.allowed
    assert verdict.reason == "budget_state_unknown"
    assert verdict.spent_usd is None, "неизвестное не должно выглядеть нулём"


def test_malformed_counter_is_unknown_not_zero(fake_redis, monkeypatch):
    """Перебитый посторонним значением ключ — это незнание, а не ноль."""
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", 10.0, raising=False)
    now = dt.datetime(2026, 9, 17, tzinfo=dt.timezone.utc)
    fake_redis.store[cost_guard._today_key(now)] = "не число"

    assert cost_guard.spent_today_usd(now) is None
    assert not cost_guard.check_budget(now).allowed


def test_unset_budget_does_not_block(fake_redis, monkeypatch):
    """Потолок не задан — предохранитель пропускает, а не рубит.

    Через BaseAgent ходят и живые /copilot-команды: fail-closed по умолчанию
    выключил бы работающее. Запрет «пайплайн без потолка» проверяется
    инвариантом на старте, а не здесь.
    """
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", 0.0, raising=False)

    verdict = cost_guard.check_budget()

    assert verdict.allowed
    assert verdict.reason == "budget_not_configured"


def test_failed_write_reports_not_recorded(broken_redis, monkeypatch):
    """Не записанная трата возвращает None — молчать об этом нельзя.

    Потерянное списание делает потолок декоративным: следующий check_budget
    его не увидит.
    """
    monkeypatch.setattr(settings, "LLM_PRICE_PER_MTOK", {"m": {"input": 3.0, "output": 0.0}}, raising=False)

    assert cost_guard.record_spend("m", 1_000_000, 0) is None


# --- инвариант включения --------------------------------------------------

def test_pipeline_cannot_be_enabled_without_budget(monkeypatch):
    """Условие включения стало исполняемым правилом, а не комментарием."""
    monkeypatch.setenv("LLM_BACKEND", "claude_cli")
    with pytest.raises(ValueError, match="LLM_DAILY_BUDGET_USD"):
        Settings(LLM_PIPELINE_ENABLED=True, LLM_DAILY_BUDGET_USD=0.0)


def test_pipeline_enabled_with_budget_is_valid(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "claude_cli")
    cfg = Settings(LLM_PIPELINE_ENABLED=True, LLM_DAILY_BUDGET_USD=25.0)
    assert cfg.LLM_DAILY_BUDGET_USD == 25.0
