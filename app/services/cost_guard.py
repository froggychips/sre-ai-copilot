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

Счётчик живёт в Redis, а не в процессе. Воркер работает prefork'ом и
рециклит форки каждые 50 задач: процесс-локальная сумма обнулялась бы
несколько раз в час, и потолок не значил бы ничего. Ключ суточный, по UTC,
с TTL — отдельная уборка не нужна.

**Fail-closed.** Недоступный Redis означает не «трать дальше», а «я не знаю,
сколько уже потрачено». Для предохранителя, который стоит между сервисом и
деньгами, незнание — причина отказать: цена ошибки несимметрична, лишний
час без RCA обратим, а потраченные деньги нет. Это отличает его от
телеметрии рядом (`ai_metrics`), которая намеренно fail-open: метрика не
важнее вызова модели, а бюджет важнее.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Dict, Optional

import structlog

from app.config import settings

log = structlog.get_logger()

#: Префикс суточного ключа. Дата — в UTC: воркеры могут стоять в разных
#: зонах, а потолок должен быть один на всех.
_KEY_PREFIX = "llm:spend:"

#: TTL с запасом на сутки — ключ переживает свой день и уходит сам.
_KEY_TTL_SECONDS = 48 * 3600

#: Расход хранится в МИКРОДОЛЛАРАХ целым числом. `INCRBYFLOAT` копит
#: ошибку двоичного округления на каждом из тысяч сложений; целое
#: `INCRBY` точен по определению.
_USD_SCALE = 1_000_000


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

    def as_dict(self) -> Dict[str, object]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "spent_usd": self.spent_usd,
            "limit_usd": self.limit_usd,
        }


def _redis():
    """Тот же клиент, что у heartbeat-ключей. Бросает, если Redis недоступен.

    Исключение НЕ глушим: вызывающий обязан отличить «потрачено 0» от
    «неизвестно сколько потрачено», а возврат None на этом уровне эти два
    случая склеивает.
    """
    from app.services.digest.state import _get_beat_redis

    return _get_beat_redis()


def _today_key(now: Optional[dt.datetime] = None) -> str:
    moment = now or dt.datetime.now(dt.timezone.utc)
    return f"{_KEY_PREFIX}{moment.strftime('%Y-%m-%d')}"


def _price_table() -> Dict[str, Dict[str, float]]:
    """Цены за миллион токенов, из настроек.

    Таблица живёт в конфиге, а не в коде: прайс меняется без нас, и
    захардкоженное число незаметно устарело бы ровно тогда, когда на него
    полагаются. Пустая таблица — законное состояние: тогда расход считается
    по `LLM_PRICE_FALLBACK_*`.
    """
    table = getattr(settings, "LLM_PRICE_PER_MTOK", None)
    return table if isinstance(table, dict) else {}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Стоимость одного вызова в долларах.

    Неизвестная модель считается по fallback-ставке, и ставка эта заведомо
    НЕ дешёвая. Ошибиться в пользу «дешевле, чем на самом деле» значит
    пропустить трату мимо потолка — то есть ровно то, от чего предохранитель
    поставлен.
    """
    prices = _price_table().get(model or "")
    if prices is None:
        in_price = float(getattr(settings, "LLM_PRICE_FALLBACK_INPUT", 15.0))
        out_price = float(getattr(settings, "LLM_PRICE_FALLBACK_OUTPUT", 75.0))
    else:
        in_price = float(prices.get("input", 0.0))
        out_price = float(prices.get("output", 0.0))

    # Отрицательные значения приходят только из битого usage; в минус
    # бюджет уводить нельзя — иначе один такой ответ «вернёт» деньги.
    tokens_in = max(0, int(input_tokens or 0))
    tokens_out = max(0, int(output_tokens or 0))
    return (tokens_in * in_price + tokens_out * out_price) / 1_000_000


def spent_today_usd(now: Optional[dt.datetime] = None) -> Optional[float]:
    """Сколько потрачено за текущие сутки UTC. None — счётчик недоступен.

    None и 0.0 — разные ответы, и различать их обязан вызывающий: первое
    значит «неизвестно», второе — «точно ничего».
    """
    try:
        raw = _redis().get(_today_key(now))
    except Exception as e:  # noqa: BLE001 — недоступность считаем незнанием
        log.warning("cost_guard.redis_unavailable", op="get", error=str(e))
        return None
    if raw is None:
        return 0.0
    try:
        return int(raw) / _USD_SCALE
    except (TypeError, ValueError):
        # Ключ перебит чем-то посторонним — это тоже незнание, не ноль.
        log.warning("cost_guard.counter_malformed", value=str(raw)[:64])
        return None


def check_budget(now: Optional[dt.datetime] = None) -> BudgetVerdict:
    """Можно ли тратить прямо сейчас.

    Проверка идёт ДО вызова модели. После — поздно: потолок был бы превышен
    ровно на стоимость последнего прогона, а у пайплайна из семи агентов
    это не округление.
    """
    limit = float(getattr(settings, "LLM_DAILY_BUDGET_USD", 0.0) or 0.0)
    if limit <= 0:
        # Потолок не задан — предохранитель молчит и пропускает.
        #
        # Отказывать здесь было бы неправильно: через `BaseAgent` ходит не
        # только пайплайн, но и живые `/copilot`-команды в Discord, и
        # fail-closed по умолчанию выключил бы работающее. Запрет «включённый
        # пайплайн без потолка» — не дело этой функции: он проверяется один
        # раз на старте инвариантом в `config.py`, где его нельзя ни забыть,
        # ни обойти. Один предохранитель — одна ответственность.
        return BudgetVerdict(
            allowed=True,
            reason="budget_not_configured",
            spent_usd=None,
            limit_usd=limit,
        )

    spent = spent_today_usd(now)
    if spent is None:
        return BudgetVerdict(
            allowed=False,
            reason="budget_state_unknown",
            spent_usd=None,
            limit_usd=limit,
        )
    if spent >= limit:
        return BudgetVerdict(
            allowed=False,
            reason="daily_budget_exhausted",
            spent_usd=spent,
            limit_usd=limit,
        )
    return BudgetVerdict(
        allowed=True,
        reason="within_budget",
        spent_usd=spent,
        limit_usd=limit,
    )


def record_spend(
    model: str,
    input_tokens: int,
    output_tokens: int,
    now: Optional[dt.datetime] = None,
) -> Optional[float]:
    """Списать стоимость вызова. Возвращает списанную сумму, None — не списали.

    Провал записи означает, что потраченное не учтено, и следующий
    `check_budget` этого не увидит. Поэтому он тут же и логируется: молча
    потерянная трата хуже отказа, потому что делает потолок декоративным.
    """
    cost = estimate_cost_usd(model, input_tokens, output_tokens)
    if cost <= 0:
        return 0.0
    micro = int(round(cost * _USD_SCALE))
    if micro <= 0:
        # Вызов дешевле микродоллара: копить нечего, но и терять нечего.
        return 0.0
    key = _today_key(now)
    try:
        client = _redis()
        pipe = client.pipeline()
        pipe.incrby(key, micro)
        pipe.expire(key, _KEY_TTL_SECONDS)
        pipe.execute()
    except Exception as e:  # noqa: BLE001
        log.warning(
            "cost_guard.spend_not_recorded",
            model=model,
            cost_usd=round(cost, 6),
            error=str(e),
        )
        return None
    return cost
