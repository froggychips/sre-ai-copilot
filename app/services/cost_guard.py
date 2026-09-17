"""Потолок расхода на LLM: предохранитель, который останавливает трату.

Зачем отдельный предохранитель, если в `settings` уже есть
`LLM_PIPELINE_ENABLED`. Тот флаг — выключатель: он либо пропускает всё,
либо не пропускает ничего. Между этими положениями нет состояния «работай,
но не дороже N долларов в сутки», а именно оно требуется, чтобы пайплайн
вообще можно было включить: арифметика из `config.py` — 50 алертов/мин ×
5 LLM-вызовов × $0.05 ≈ $750/час до того, как кто-то заметит.

Cap в консоли Anthropic эту задачу не решает. Он рубит ключ целиком и
постфактум: узнать о превышении можно, когда уже потрачено, а вместе с
пайплайном умрёт всё остальное, что ходит тем же ключом. Предохранитель
нужен здесь — до вызова и с отказом ровно одного потребителя.

РЕЗЕРВИРОВАНИЕ, А НЕ ПРОВЕРКА
-----------------------------
Наивное «посмотреть сумму и пропустить, если меньше потолка» потолком не
является. Вызов, начатый при $9.99 из $10, закончится выше предела; хуже
того, `MultiHypothesisAgent.generate` делает вызовы через `asyncio.gather`,
а воркеров несколько — все они увидят один и тот же баланс раньше, чем
первый из них успеет списать. Поэтому стоимость **резервируется до
вызова** одной атомарной операцией, а после вызова резерв сводится с
фактическим расходом. Отказ и списание — это одно и то же действие, а не
два разных.

ПОЧЕМУ POSTGRES, А НЕ REDIS
---------------------------
Redis в этом кластере поднят с `maxmemory 256mb` и `allkeys-lru`
(`k8s/redis.yaml`): под давлением памяти он вытеснит любой ключ, включая
счётчик расхода. Исчезнувший счётчик читается как «потрачено 0» и выдаёт
полный суточный бюджет заново — то есть предохранитель открывается сам,
тихо и именно тогда, когда система под нагрузкой. Деньгам нужна durable
запись, а не кэш.

Побочно это чинит и второй способ открыться: Redis, который читается, но
не принимает запись (read-only реплика, кончился диск под AOF), оставлял
бы `check` довольным устаревшей суммой, пока каждая следующая трата уходит
в никуда. Здесь резерв — это запись, и провал записи означает отказ.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any, Dict, Optional

import structlog
from sqlalchemy import text

from app.config import settings

log = structlog.get_logger()

#: Расход хранится в МИКРОДОЛЛАРАХ целым числом. Дробное сложение копит
#: ошибку двоичного округления на тысячах операций, а целое точно.
_USD_SCALE = 1_000_000

#: Имя таблицы для читателя и тестов; в сами запросы оно вписано
#: литералом (см. _apply_delta).
_TABLE = "llm_spend_ledger"


class LLMBudgetExceeded(Exception):
    """Вызов модели запрещён предохранителем бюджета.

    Намеренно НЕ входит в `RETRIABLE_EXC` (app/workers/tasks.py): ретрай при
    исчерпанном потолке — это повторная попытка потратить то, чего тратить
    нельзя, умноженная на число попыток.
    """


@dataclass(frozen=True)
class BudgetVerdict:
    """Решение предохранителя. `allowed=False` — вызывать модель нельзя."""

    allowed: bool
    reason: str
    spent_usd: Optional[float]
    limit_usd: float
    #: Сколько зарезервировано под этот вызов. Сводится в `settle`.
    reserved_usd: float = 0.0
    #: Сутки, со счётчика которых списан резерв. Сведение обязано идти в ЭТУ
    #: строку, а не в «сегодня»: вызов, начатый в 23:59 и закончившийся в
    #: 00:01, иначе возвращал бы излишек новому дню (где его клампит ноль),
    #: а на старом дне навсегда оставался бы полный worst-case.
    day: Optional[dt.date] = None

    def as_dict(self) -> Dict[str, object]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "spent_usd": self.spent_usd,
            "limit_usd": self.limit_usd,
            "reserved_usd": self.reserved_usd,
            "day": self.day.isoformat() if self.day else None,
        }


def _today(now: Optional[dt.datetime] = None) -> dt.date:
    """Сутки по UTC: воркеры могут стоять в разных зонах, потолок один."""
    moment = now or dt.datetime.now(dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc).date()


def _limit_usd() -> float:
    """Потолок из настроек. 0 — не задан.

    NaN и inf отсеиваются здесь, а не только инвариантом конфига: настройку
    можно подменить в рантайме (тесты, `monkeypatch`), а сравнение с NaN
    всегда ложно — то есть такой «потолок» пропускал бы вообще всё.
    """
    raw = getattr(settings, "LLM_DAILY_BUDGET_USD", 0.0) or 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value) or value <= 0:
        return 0.0
    return value


def _price_table() -> Dict[str, Dict[str, float]]:
    """Цены за миллион токенов, из настроек.

    Таблица живёт в конфиге, а не в коде: прайс меняется без нас, и
    захардкоженное число незаметно устарело бы ровно тогда, когда на него
    полагаются.
    """
    table = getattr(settings, "LLM_PRICE_PER_MTOK", None)
    return table if isinstance(table, dict) else {}


def _valid_rate(value: Any) -> Optional[float]:
    """Ставка, если она вообще является ставкой. Иначе None.

    Отвергается всё, по чему нельзя честно посчитать деньги: не-число,
    NaN, бесконечность, ноль и отрицательное. Ноль отвергается наравне с
    мусором намеренно — «бесплатная модель» и «эту графу забыли заполнить»
    выглядят в конфиге одинаково, а стоят по-разному.
    """
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(rate) or rate <= 0:
        return None
    return rate


def _rates_for(model: str) -> tuple:
    """Ставки (input, output) за миллион токенов для модели.

    Запись, у которой не хватает половины или ставка не годится, считается
    НЕ настроенной — берётся дорогой fallback. Иначе `{"input": 3}`
    означало бы бесплатный выход, и это был бы худший вид ошибки: потолок
    на месте, цифры правдоподобны, а половина расхода не считается.

    Такие записи ещё и не дают процессу подняться с включённым пайплайном
    (инвариант в `config.py`) — но настройку можно подменить в рантайме, и
    здесь стоит вторая линия.
    """
    fallback_in = _valid_rate(getattr(settings, "LLM_PRICE_FALLBACK_INPUT", None)) or 15.0
    fallback_out = _valid_rate(getattr(settings, "LLM_PRICE_FALLBACK_OUTPUT", None)) or 75.0

    prices = _price_table().get(model or "")
    if not isinstance(prices, dict):
        return fallback_in, fallback_out

    in_price = _valid_rate(prices.get("input"))
    out_price = _valid_rate(prices.get("output"))
    if in_price is None or out_price is None:
        log.warning(
            "cost_guard.price_entry_invalid",
            model=model,
            entry=str(prices)[:120],
            note="считаем по дорогой ставке: половина цены хуже отсутствия цены",
        )
        return fallback_in, fallback_out
    return in_price, out_price


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Стоимость вызова в долларах.

    Неизвестная модель считается по fallback-ставке, и ставка эта заведомо
    НЕ дешёвая. Ошибиться в пользу «дешевле, чем на самом деле» значит
    пропустить трату мимо потолка — то есть ровно то, от чего предохранитель
    поставлен.
    """
    in_price, out_price = _rates_for(model)

    # Отрицательные значения приходят только из битого usage; в минус
    # бюджет уводить нельзя — иначе один такой ответ «вернёт» деньги.
    tokens_in = max(0, int(input_tokens or 0))
    tokens_out = max(0, int(output_tokens or 0))
    return (tokens_in * in_price + tokens_out * out_price) / 1_000_000


def estimate_worst_case_usd(model: str, prompt: str) -> float:
    """Верхняя оценка стоимости вызова, известная ДО вызова.

    Вход считается в БАЙТАХ UTF-8, и это не приблизительность, а граница,
    которую можно доказать: любой токен субсловного токенизатора занимает
    минимум один байт исходного текста, поэтому токенов никогда не больше,
    чем байт. Прежняя оценка «символов / 3» границей не была — она
    подобрана под латиницу и кириллицу, а на CJK, эмодзи, base64 и прочем
    плотном тексте занижает счёт. Заниженный резерв не защищает: он
    пропускает вызов, который пробьёт потолок, и `settle` потом честно
    допишет перерасход — уже после того, как деньги потрачены.

    Выход берётся равным `MAX_TOKENS`: больше модели выдать не разрешено.

    Оценка щедрая (для английского текста — вчетверо), и это нормально:
    завышенный резерв лишь остановит раньше, а разница возвращается сразу
    после ответа. Настоящую цифру знает только токенизатор провайдера, и
    когда счёт станет узким местом, за ней надо идти к нему.
    """
    prompt_bytes = len((prompt or "").encode("utf-8"))
    output_tokens = int(getattr(settings, "MAX_TOKENS", 4096) or 4096)
    return estimate_cost_usd(model, prompt_bytes, output_tokens)


def _apply_delta(day: dt.date, delta_micro: int) -> int:
    """Прибавить к суточному счётчику и вернуть новое значение.

    Одна атомарная операция: UPSERT со сложением на стороне БД. Прочитать,
    сложить в питоне и записать значило бы вернуть ровно ту гонку, ради
    которой всё это и делается.

    Счётчик не опускается ниже нуля: возврат неиспользованного резерва не
    должен «печатать» бюджет, если списания разъехались.
    """
    from app.database import SessionLocal

    # Имя таблицы — литералом, а не через f-строку: подставлять в SQL хоть
    # что-то форматированием здесь незачем (таблица одна и известна), а
    # статический анализ иначе справедливо считает это конструированием
    # запроса из строк (bandit B608).
    sql = text("""
        INSERT INTO llm_spend_ledger (day, spent_micro_usd, updated_at)
        VALUES (:day, GREATEST(:delta, 0), NOW())
        ON CONFLICT (day) DO UPDATE
        SET spent_micro_usd = GREATEST(llm_spend_ledger.spent_micro_usd + :delta, 0),
            updated_at = NOW()
        RETURNING spent_micro_usd
    """)
    db = SessionLocal()
    try:
        value = db.execute(sql, {"day": day, "delta": delta_micro}).scalar_one()
        db.commit()
        return int(value)
    finally:
        db.close()


def _read_spent_micro(day: dt.date) -> Optional[int]:
    from app.database import SessionLocal

    sql = text("SELECT spent_micro_usd FROM llm_spend_ledger WHERE day = :day")
    db = SessionLocal()
    try:
        row = db.execute(sql, {"day": day}).scalar()
        return int(row) if row is not None else 0
    finally:
        db.close()


def spent_today_usd(now: Optional[dt.datetime] = None) -> Optional[float]:
    """Сколько потрачено за текущие сутки UTC. None — счётчик недоступен.

    None и 0.0 — разные ответы, и различать их обязан вызывающий: первое
    значит «неизвестно», второе — «точно ничего».
    """
    try:
        micro = _read_spent_micro(_today(now))
    except Exception as e:  # noqa: BLE001 — недоступность считаем незнанием
        log.warning("cost_guard.ledger_unavailable", op="read", error=str(e))
        return None
    return None if micro is None else micro / _USD_SCALE


def reserve(
    model: str,
    prompt: str,
    now: Optional[dt.datetime] = None,
) -> BudgetVerdict:
    """Зарезервировать стоимость вызова. `allowed=False` — вызывать нельзя.

    Резерв списывается ДО обращения к модели и сводится с фактическим
    расходом в `settle`. Проверка без резерва потолком не является: между
    ней и списанием помещается сколько угодно параллельных вызовов.
    """
    limit = _limit_usd()
    if limit <= 0:
        # Потолок не задан — предохранитель молчит и пропускает.
        #
        # Отказывать здесь было бы неправильно: через `BaseAgent` ходит не
        # только пайплайн, но и живые `/copilot`-команды, и fail-closed по
        # умолчанию выключил бы работающее. Запрет «включённый пайплайн без
        # потолка» — не дело этой функции: он проверяется один раз на старте
        # инвариантом в `config.py`, где его нельзя ни забыть, ни обойти.
        return BudgetVerdict(True, "budget_not_configured", None, 0.0)

    cost = estimate_worst_case_usd(model, prompt)
    micro = max(1, int(math.ceil(cost * _USD_SCALE)))
    day = _today(now)

    try:
        new_total_micro = _apply_delta(day, micro)
    except Exception as e:  # noqa: BLE001
        # Резерв не записан — значит неизвестно, сколько потрачено, и
        # разрешать нечем. Это и есть fail-closed: у предохранителя между
        # сервисом и деньгами незнание равно запрету.
        log.warning("cost_guard.reserve_failed", model=model, error=str(e))
        return BudgetVerdict(False, "budget_state_unknown", None, limit)

    new_total = new_total_micro / _USD_SCALE
    if new_total > limit:
        # Резерв не помещается в потолок — откатываем его и отказываем.
        # Откат обязателен: иначе отклонённые вызовы съедали бы бюджет и
        # предохранитель захлопнулся бы навсегда после первого же отказа.
        try:
            _apply_delta(day, -micro)
        except Exception as e:  # noqa: BLE001
            log.warning("cost_guard.reserve_rollback_failed", error=str(e))
        return BudgetVerdict(
            False, "daily_budget_exhausted", new_total - cost, limit
        )

    return BudgetVerdict(
        True, "within_budget", new_total, limit, reserved_usd=cost, day=day
    )


def settle(
    verdict: BudgetVerdict,
    model: str,
    input_tokens: int,
    output_tokens: int,
    now: Optional[dt.datetime] = None,
) -> float:
    """Свести резерв с фактом. Возвращает сумму, УЧТЁННУЮ в счётчике.

    Вызывается ПОСЛЕ ответа модели, включая ответы неудачные: пустой и
    обрезанный тоже оплачены. Разница между резервом и фактом возвращается
    в бюджет — именно поэтому резерв может быть щедрым.

    Возвращается не «фактическая стоимость», а то, что на самом деле легло
    в счётчик. Разница видна, когда свести не удалось: в счётчике остаётся
    полный резерв, и отдать вызывающему меньшее число значило бы развести
    метрику расхода с ledger ровно в момент отказа хранилища.

    Сведение идёт в сутки, с которых списан резерв (`verdict.day`), а не в
    текущие: вызов, начатый в 23:59 и закончившийся в 00:01, иначе вернул
    бы излишек новому дню — где отрицательная дельта упирается в ноль, —
    а на старом дне навсегда остался бы полный worst-case.
    """
    if verdict.reserved_usd <= 0:
        # Резерва не было (потолок не задан) — сводить нечего.
        return estimate_cost_usd(model, input_tokens, output_tokens)

    actual = estimate_cost_usd(model, input_tokens, output_tokens)
    delta_micro = int(round((actual - verdict.reserved_usd) * _USD_SCALE))
    if delta_micro == 0:
        return actual
    day = verdict.day or _today(now)
    try:
        _apply_delta(day, delta_micro)
    except Exception as e:  # noqa: BLE001
        # Свести не удалось: в счётчике остался полный резерв. Возвращаем
        # его, а не факт — иначе метрика показала бы меньше, чем списано.
        log.warning(
            "cost_guard.settle_failed",
            model=model, delta_usd=round(actual - verdict.reserved_usd, 6),
            error=str(e),
        )
        return verdict.reserved_usd
    return actual


def release(verdict: BudgetVerdict, now: Optional[dt.datetime] = None) -> None:
    """Вернуть резерв целиком — попытка ТОЧНО не была оплачена.

    Применимо к узкому случаю: провайдер отказал до обработки запроса
    (429 rate limit). Там, где ответ мог быть сгенерирован и не доехать
    (таймаут), резерв остаётся списанным: считать такую попытку бесплатной
    значит открывать потолок ровно на неудачных прогонах, которых при
    проблемах с провайдером больше всего.

    Без этого возврата шторм 429 — отказы, за которые никто не платит, —
    съедал бы суточный бюджет и блокировал день.
    """
    if verdict.reserved_usd <= 0:
        return
    micro = int(round(verdict.reserved_usd * _USD_SCALE))
    # В сутки резерва, а не в текущие: 429 после полуночи иначе вычитался бы
    # из нового дня, оставив вчерашний с полным резервом.
    day = verdict.day or _today(now)
    try:
        _apply_delta(day, -micro)
    except Exception as e:  # noqa: BLE001
        log.warning("cost_guard.release_failed", error=str(e))


def peek(now: Optional[dt.datetime] = None) -> BudgetVerdict:
    """Состояние бюджета БЕЗ резервирования — «стоит ли вообще начинать».

    Гарантией потолка не является и не пытается быть: между этим ответом и
    вызовом модели помещается что угодно. Гарантию даёт `reserve`, а это —
    способ не начинать прогон из семи агентов, когда деньги уже кончились,
    и не платить за первый из них, чтобы это выяснить.
    """
    limit = _limit_usd()
    if limit <= 0:
        return BudgetVerdict(True, "budget_not_configured", None, 0.0)

    spent = spent_today_usd(now)
    if spent is None:
        return BudgetVerdict(False, "budget_state_unknown", None, limit)
    if spent >= limit:
        return BudgetVerdict(False, "daily_budget_exhausted", spent, limit)
    return BudgetVerdict(True, "within_budget", spent, limit)
