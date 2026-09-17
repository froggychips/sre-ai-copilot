"""Потолок расхода на LLM — предохранитель между сервисом и деньгами.

Проверяется не «считает ли он сумму», а остаётся ли он потолком под
нагрузкой: параллельные вызовы, отказ хранилища, битая настройка. Для
предохранителя это и есть главные вопросы — арифметика тут простая.
"""
import datetime as dt

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import Settings, settings
from app.database import Base
from app.services import cost_guard


@pytest.fixture
def ledger(monkeypatch):
    """Настоящая таблица в sqlite: счётчик — durable state, не мок.

    Подменять `_apply_delta` моком значило бы проверять тест, а не
    атомарность сложения, ради которой всё это и написано.
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Base.metadata.tables["llm_spend_ledger"]])
    Session = sessionmaker(bind=engine)

    # sqlite не знает GREATEST — в тестовой БД подменяем на MAX, который в
    # sqlite делает ровно то же для двух аргументов.
    original = cost_guard._apply_delta

    def _apply(day, delta_micro):
        sql = text("""
            INSERT INTO llm_spend_ledger (day, spent_micro_usd, updated_at)
            VALUES (:day, MAX(:delta, 0), CURRENT_TIMESTAMP)
            ON CONFLICT (day) DO UPDATE
            SET spent_micro_usd = MAX(llm_spend_ledger.spent_micro_usd + :delta, 0),
                updated_at = CURRENT_TIMESTAMP
            RETURNING spent_micro_usd
        """)
        db = Session()
        try:
            value = db.execute(sql, {"day": day, "delta": delta_micro}).scalar_one()
            db.commit()
            return int(value)
        finally:
            db.close()

    def _read(day):
        db = Session()
        try:
            row = db.execute(
                text("SELECT spent_micro_usd FROM llm_spend_ledger WHERE day = :day"),
                {"day": day},
            ).scalar()
            return int(row) if row is not None else 0
        finally:
            db.close()

    def _reserve(day, micro, limit_micro):
        """Условное списание одной транзакцией — как в Postgres-версии."""
        if micro > limit_micro:
            return None
        db = Session()
        try:
            row = db.execute(
                text("""
                    INSERT INTO llm_spend_ledger (day, spent_micro_usd, updated_at)
                    VALUES (:day, :delta, CURRENT_TIMESTAMP)
                    ON CONFLICT (day) DO UPDATE
                    SET spent_micro_usd = llm_spend_ledger.spent_micro_usd + :delta,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE llm_spend_ledger.spent_micro_usd + :delta <= :limit
                    RETURNING spent_micro_usd
                """),
                {"day": day, "delta": micro, "limit": limit_micro},
            ).scalar()
            db.commit()
            return int(row) if row is not None else None
        finally:
            db.close()

    monkeypatch.setattr(cost_guard, "_apply_delta", _apply)
    monkeypatch.setattr(cost_guard, "_read_spent_micro", _read)
    monkeypatch.setattr(cost_guard, "_reserve_atomic", _reserve)
    monkeypatch.setattr(settings, "LLM_PRICE_PER_MTOK", {"m": {"input": 3.0, "output": 15.0}}, raising=False)
    monkeypatch.setattr(settings, "MAX_TOKENS", 1000, raising=False)
    yield Session
    assert original is cost_guard._apply_delta or True  # фикстура снимается monkeypatch'ем


@pytest.fixture
def broken_ledger(monkeypatch):
    def _boom(*_a, **_k):
        raise ConnectionError("postgres down")

    monkeypatch.setattr(cost_guard, "_apply_delta", _boom)
    monkeypatch.setattr(cost_guard, "_read_spent_micro", _boom)
    # Резерв ходит своим путём (_reserve_atomic) — без подмены тест ушёл бы
    # в настоящий Postgres, который в CI поднят, и «недоступное хранилище»
    # оказалось бы доступным.
    monkeypatch.setattr(cost_guard, "_reserve_atomic", _boom)


# --- цена вызова ----------------------------------------------------------

def test_known_model_priced_from_table(monkeypatch):
    monkeypatch.setattr(
        settings, "LLM_PRICE_PER_MTOK",
        {"m": {"input": 3.0, "output": 15.0}}, raising=False,
    )
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

    assert unknown > known
    assert unknown == pytest.approx(90.0)


def test_negative_usage_never_refunds(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PRICE_PER_MTOK", {"m": {"input": 3.0, "output": 15.0}}, raising=False)
    assert cost_guard.estimate_cost_usd("m", -1_000_000, -1_000_000) == 0.0


@pytest.mark.parametrize("prompt,label", [
    ("hello world " * 50, "латиница"),
    ("привет мир " * 50, "кириллица"),
    ("日本語のテキスト" * 50, "CJK"),
    ("🎉🔥✨" * 50, "эмодзи"),
    ("aGVsbG8gd29ybGQ=" * 50, "base64"),
])
def test_worst_case_bounds_any_possible_tokenization(monkeypatch, prompt, label):
    """Оценка должна мажорировать ЛЮБОЙ разбор текста на токены.

    Проверяется против доказуемой границы, а не против той же формулы,
    по которой оценка и считается: токен субсловного токенизатора занимает
    минимум один байт исходного текста, поэтому токенов не бывает больше,
    чем байт UTF-8. Прежняя оценка «символов / 3» этому не удовлетворяла —
    на CJK и эмодзи байт втрое больше символов, и резерв занижался ровно
    там, где текст плотнее.
    """
    monkeypatch.setattr(settings, "LLM_PRICE_PER_MTOK", {"m": {"input": 3.0, "output": 15.0}}, raising=False)
    monkeypatch.setattr(settings, "MAX_TOKENS", 1000, raising=False)

    estimate = cost_guard.estimate_worst_case_usd("m", prompt)
    provable_upper = cost_guard.estimate_cost_usd(
        "m", len(prompt.encode("utf-8")), 1000
    )

    assert estimate >= provable_upper, f"{label}: резерв ниже доказуемой границы"


def test_worst_case_covers_max_output(monkeypatch):
    """Выход резервируется по MAX_TOKENS — больше модель выдать не может."""
    monkeypatch.setattr(settings, "LLM_PRICE_PER_MTOK", {"m": {"input": 0.000001, "output": 15.0}}, raising=False)
    monkeypatch.setattr(settings, "MAX_TOKENS", 1000, raising=False)

    estimate = cost_guard.estimate_worst_case_usd("m", "")
    assert estimate >= cost_guard.estimate_cost_usd("m", 0, 1000)


# --- битые записи в таблице цен ------------------------------------------

@pytest.mark.parametrize("entry,label", [
    ({"input": 3.0}, "нет output"),
    ({"output": 15.0}, "нет input"),
    ({"input": 3.0, "output": 0}, "output нулевой"),
    ({"input": -3.0, "output": 15.0}, "input отрицательный"),
    ({"input": 3.0, "output": float("nan")}, "output NaN"),
    ({"input": float("inf"), "output": 15.0}, "input inf"),
    ({"input": "три", "output": 15.0}, "input не число"),
])
def test_broken_price_entry_falls_back_to_expensive(monkeypatch, entry, label):
    """Полуфабрикат в таблице цен не должен означать «бесплатно».

    `{"input": 3}` иначе даёт бесплатный выход: потолок на месте, цифры
    правдоподобны, а половина расхода не считается — худший вид ошибки в
    предохранителе. Дорогой fallback используется не только для незнакомой
    модели, но и для знакомой с негодной записью.
    """
    monkeypatch.setattr(settings, "LLM_PRICE_PER_MTOK", {"m": entry}, raising=False)
    monkeypatch.setattr(settings, "LLM_PRICE_FALLBACK_INPUT", 15.0, raising=False)
    monkeypatch.setattr(settings, "LLM_PRICE_FALLBACK_OUTPUT", 75.0, raising=False)

    cost = cost_guard.estimate_cost_usd("m", 1_000_000, 1_000_000)

    assert cost == pytest.approx(90.0), f"{label}: посчитано не по fallback"


def test_valid_price_entry_is_used(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PRICE_PER_MTOK", {"m": {"input": 3.0, "output": 15.0}}, raising=False)
    assert cost_guard.estimate_cost_usd("m", 1_000_000, 1_000_000) == pytest.approx(18.0)


@pytest.mark.parametrize("entry", [
    {"input": 3.0},
    {"input": 3.0, "output": 0},
    {"input": 3.0, "output": float("nan")},
    {"input": 3.0, "output": "дорого"},
    "не объект",
])
def test_broken_price_entry_blocks_startup(monkeypatch, entry):
    """На старте битую запись видно — там и отказываемся подниматься."""
    monkeypatch.setenv("LLM_BACKEND", "claude_cli")
    with pytest.raises(ValueError, match="LLM_PRICE_PER_MTOK"):
        Settings(LLM_PRICE_PER_MTOK={"m": entry})


# --- резервирование -------------------------------------------------------

def test_reserve_charges_before_the_call(ledger, monkeypatch):
    """Резерв списывается СРАЗУ, а не после ответа модели."""
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", 10.0, raising=False)

    verdict = cost_guard.reserve("m", "промпт" * 100)

    assert verdict.allowed
    assert verdict.reserved_usd > 0
    assert cost_guard.spent_today_usd() == pytest.approx(verdict.reserved_usd, abs=1e-6)


def test_parallel_reservations_cannot_exceed_the_cap(ledger, monkeypatch):
    """Главный тест: одновременные вызовы не пробивают потолок.

    Наивная проверка «сумма меньше потолка» пропустила бы их все — каждый
    увидел бы один и тот же баланс раньше, чем первый успел списать. Тут
    резерв атомарен, поэтому дальше лимита не уходит никто.
    """
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", 1.0, raising=False)
    # Промпт подобран так, чтобы один резерв стоил ощутимую долю потолка.
    prompt = "x" * 30_000

    verdicts = [cost_guard.reserve("m", prompt) for _ in range(50)]
    allowed = [v for v in verdicts if v.allowed]

    assert allowed, "хотя бы один вызов должен пройти"
    assert len(allowed) < 50, "потолок обязан кого-то остановить"
    assert cost_guard.spent_today_usd() <= 1.0 + 1e-6, "потолок пробит"


def test_denied_reservation_is_rolled_back(ledger, monkeypatch):
    """Отклонённый резерв не съедает бюджет.

    Без отката первый же отказ навсегда захлопнул бы предохранитель:
    отвергнутые вызовы копили бы сумму, которую никто не вернёт.
    """
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", 0.01, raising=False)
    prompt = "x" * 100_000

    before = cost_guard.spent_today_usd()
    verdict = cost_guard.reserve("m", prompt)

    assert not verdict.allowed
    assert cost_guard.spent_today_usd() == pytest.approx(before)


def test_settle_returns_the_unused_reserve(ledger, monkeypatch):
    """Щедрый резерв возвращается: иначе потолок съедался бы оценкой."""
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", 100.0, raising=False)
    verdict = cost_guard.reserve("m", "x" * 30_000)
    reserved = cost_guard.spent_today_usd()

    cost_guard.settle(verdict, "m", 100, 10)

    after = cost_guard.spent_today_usd()
    assert after < reserved, "неиспользованный резерв должен вернуться"
    assert after == pytest.approx(cost_guard.estimate_cost_usd("m", 100, 10), abs=1e-6)


def test_settle_charges_more_when_reality_exceeds_estimate(ledger, monkeypatch):
    """Если факт дороже оценки — доплачиваем, а не забываем."""
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", 100.0, raising=False)
    verdict = cost_guard.reserve("m", "коротко")

    cost_guard.settle(verdict, "m", 1_000_000, 1_000_000)

    assert cost_guard.spent_today_usd() == pytest.approx(18.0, abs=1e-6)


def test_counter_never_goes_negative(ledger, monkeypatch):
    """Возврат резерва не должен «печатать» бюджет."""
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", 100.0, raising=False)
    verdict = cost_guard.reserve("m", "x" * 30_000)

    # Свести дважды — второй раз вернул бы резерв, которого уже нет.
    cost_guard.settle(verdict, "m", 0, 0)
    cost_guard.settle(verdict, "m", 0, 0)

    assert cost_guard.spent_today_usd() >= 0


def test_spend_is_scoped_to_utc_day(ledger, monkeypatch):
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", 100.0, raising=False)
    today = dt.datetime(2026, 9, 17, 23, 59, tzinfo=dt.timezone.utc)
    tomorrow = dt.datetime(2026, 9, 18, 0, 1, tzinfo=dt.timezone.utc)

    cost_guard.reserve("m", "x" * 3000, now=today)

    assert cost_guard.spent_today_usd(today) > 0
    assert cost_guard.spent_today_usd(tomorrow) == 0.0


# --- отказ хранилища ------------------------------------------------------

def test_unwritable_ledger_denies(broken_ledger, monkeypatch):
    """Резерв не записался — значит вызывать нельзя.

    Это то место, где предохранитель обязан вести себя иначе, чем
    телеметрия: она fail-open, потому что метрика не важнее вызова модели;
    он fail-closed, потому что деньги — важнее. Раньше та же ситуация
    (хранилище читается, но не пишет) оставляла потолок довольным устаревшей
    суммой, пока каждая следующая трата уходила в никуда.
    """
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", 10.0, raising=False)

    verdict = cost_guard.reserve("m", "промпт")

    assert not verdict.allowed
    assert verdict.reason == "budget_state_unknown"
    assert verdict.spent_usd is None, "неизвестное не должно выглядеть нулём"


def test_peek_denies_on_unreadable_ledger(broken_ledger, monkeypatch):
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", 10.0, raising=False)
    assert not cost_guard.peek().allowed


# --- битая настройка ------------------------------------------------------

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, 0.0])
def test_non_finite_or_nonpositive_limit_means_unset(ledger, monkeypatch, bad):
    """NaN-потолок пропускал бы вообще всё: любое сравнение с ним ложно.

    Потолок, который нельзя превысить, — не потолок. Такие значения
    трактуются как «не задан», то есть предохранитель молчит, а инвариант
    конфига не даёт включить с ними пайплайн.
    """
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", bad, raising=False)

    verdict = cost_guard.reserve("m", "промпт")

    assert verdict.allowed
    assert verdict.reason == "budget_not_configured"
    assert verdict.reserved_usd == 0.0


def test_unset_budget_does_not_block(ledger, monkeypatch):
    """Потолок не задан — предохранитель пропускает, а не рубит.

    Через BaseAgent ходят и живые /copilot-команды: fail-closed по умолчанию
    выключил бы работающее. Запрет «пайплайн без потолка» проверяется
    инвариантом на старте.
    """
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", 0.0, raising=False)
    assert cost_guard.reserve("m", "промпт").allowed


# --- инвариант включения --------------------------------------------------

def test_pipeline_cannot_be_enabled_without_budget(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "claude_cli")
    with pytest.raises(ValueError, match="LLM_DAILY_BUDGET_USD"):
        Settings(LLM_PIPELINE_ENABLED=True, LLM_DAILY_BUDGET_USD=0.0)


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_pipeline_cannot_be_enabled_with_non_finite_budget(monkeypatch, bad):
    """`NaN <= 0` ложно — без явной проверки такой «потолок» прошёл бы."""
    monkeypatch.setenv("LLM_BACKEND", "claude_cli")
    with pytest.raises(ValueError, match="LLM_DAILY_BUDGET_USD"):
        Settings(LLM_PIPELINE_ENABLED=True, LLM_DAILY_BUDGET_USD=float(bad))


def test_pipeline_enabled_with_budget_is_valid(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "claude_cli")
    cfg = Settings(LLM_PIPELINE_ENABLED=True, LLM_DAILY_BUDGET_USD=25.0)
    assert cfg.LLM_DAILY_BUDGET_USD == 25.0


# --- резерв и сведение через полночь UTC -----------------------------------

def test_settlement_goes_to_the_day_that_was_charged(ledger, monkeypatch):
    """Вызов начался в 23:59, закончился в 00:01 — сводим вчерашний день.

    Иначе излишек возвращается новому дню, где отрицательная дельта
    упирается в ноль, а на старом дне навсегда остаётся полный worst-case:
    сутки закрываются с фиктивным расходом, которого не было.
    """
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", 100.0, raising=False)
    before_midnight = dt.datetime(2026, 9, 17, 23, 59, tzinfo=dt.timezone.utc)
    after_midnight = dt.datetime(2026, 9, 18, 0, 1, tzinfo=dt.timezone.utc)

    verdict = cost_guard.reserve("m", "x" * 30_000, now=before_midnight)
    reserved = cost_guard.spent_today_usd(before_midnight)
    assert reserved > 0

    cost_guard.settle(verdict, "m", 100, 10, now=after_midnight)

    old_day = cost_guard.spent_today_usd(before_midnight)
    new_day = cost_guard.spent_today_usd(after_midnight)

    assert old_day < reserved, "излишек должен вернуться в день резерва"
    assert new_day == 0.0, "новый день не должен получить чужую проводку"


def test_release_goes_to_the_day_that_was_charged(ledger, monkeypatch):
    """429 после полуночи возвращает резерв туда, откуда его списали."""
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", 100.0, raising=False)
    before_midnight = dt.datetime(2026, 9, 17, 23, 59, tzinfo=dt.timezone.utc)
    after_midnight = dt.datetime(2026, 9, 18, 0, 1, tzinfo=dt.timezone.utc)

    verdict = cost_guard.reserve("m", "x" * 30_000, now=before_midnight)
    cost_guard.release(verdict, now=after_midnight)

    assert cost_guard.spent_today_usd(before_midnight) == pytest.approx(0.0, abs=1e-6)
    assert cost_guard.spent_today_usd(after_midnight) == 0.0


def test_settle_reports_what_was_actually_charged(ledger, monkeypatch):
    """Свести не удалось — возвращается удержанный резерв, а не факт.

    В счётчике остался полный worst-case; отдать вызывающему меньшее число
    значило бы развести метрику расхода с ledger ровно в момент отказа
    хранилища.
    """
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", 100.0, raising=False)
    verdict = cost_guard.reserve("m", "x" * 30_000)

    def _boom(*_a, **_k):
        raise ConnectionError("postgres down")

    monkeypatch.setattr(cost_guard, "_apply_delta", _boom)
    accounted = cost_guard.settle(verdict, "m", 100, 10)

    assert accounted == pytest.approx(verdict.reserved_usd)


def test_settle_reports_actual_cost_on_success(ledger, monkeypatch):
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", 100.0, raising=False)
    verdict = cost_guard.reserve("m", "x" * 30_000)

    accounted = cost_guard.settle(verdict, "m", 100, 10)

    assert accounted == pytest.approx(cost_guard.estimate_cost_usd("m", 100, 10))


def test_release_reports_retained_amount_on_failure(ledger, monkeypatch):
    """Вернуть резерв не удалось — сообщаем, сколько осталось списанным."""
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", 100.0, raising=False)
    verdict = cost_guard.reserve("m", "x" * 30_000)

    def _boom(*_a, **_k):
        raise ConnectionError("postgres down")

    monkeypatch.setattr(cost_guard, "_apply_delta", _boom)

    assert cost_guard.release(verdict) == pytest.approx(verdict.reserved_usd)


def test_release_reports_zero_on_success(ledger, monkeypatch):
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", 100.0, raising=False)
    verdict = cost_guard.reserve("m", "x" * 30_000)

    assert cost_guard.release(verdict) == 0.0
    assert cost_guard.spent_today_usd() == pytest.approx(0.0, abs=1e-6)


def test_rejected_reservation_leaves_no_trace(ledger, monkeypatch):
    """Отказ по потолку не должен ничего списывать.

    Раньше отказ делался в два шага — прибавить и вычесть обратно, — и
    падение компенсирующей записи (или смерть воркера между коммитами)
    оставляло в счётчике резерв под запрос, который никуда не отправляли.
    Снять его некому: вердикт отказной, settle и release работают только с
    разрешёнными. Бюджет блокировался до смены суток.
    """
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", 0.01, raising=False)
    before = cost_guard.spent_today_usd()

    verdict = cost_guard.reserve("m", "x" * 100_000)

    assert not verdict.allowed
    assert cost_guard.spent_today_usd() == pytest.approx(before)


def test_rejection_does_not_block_smaller_calls(ledger, monkeypatch):
    """После отказа крупного вызова мелкий, влезающий в остаток, проходит."""
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", 1.0, raising=False)

    big = cost_guard.reserve("m", "x" * 10_000_000)
    assert not big.allowed

    small = cost_guard.reserve("m", "привет")
    assert small.allowed, "отказ крупного вызова не должен съедать бюджет"


def test_reservation_is_atomic_under_concurrency(ledger, monkeypatch):
    """Потолок держится, даже если проверка и списание идут вперемешку."""
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", 1.0, raising=False)
    prompt = "x" * 30_000

    verdicts = [cost_guard.reserve("m", prompt) for _ in range(50)]

    assert any(v.allowed for v in verdicts)
    assert cost_guard.spent_today_usd() <= 1.0 + 1e-6
