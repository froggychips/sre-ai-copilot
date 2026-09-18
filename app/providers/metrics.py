"""`MetricsProvider` — интерфейс к метрикам, независимый от хранилища.

Второй провайдер после логов, и берётся он не по порядку в списке, а по
найденному дефекту. `VMClient.query_instant` честно различает «нет данных»
и «ноль» — в его докстринге это прямо расписано. А два соседних метода,
`query_instant_by` и `query_instant_by_labels`, при любой ошибке
возвращают пустой словарь: «у сервисов нет метрик» и «VictoriaMetrics не
ответила» становятся одним ответом. Два соглашения об отказе внутри одного
клиента.

Что из этого следует на практике. `metrics_sync` собирает пять метрик на
namespace через `query_instant_by`. Ошибка глотается внутри клиента, до
`except` в синке дело не доходит, счётчик `errors` остаётся нулевым, а
сервисы получают `None` по всем метрикам и уходят в `skipped_empty`. При
полностью недоступной VictoriaMetrics прогон выглядит образцовым:
`errors=0`, `inserted=0`, и правдоподобная цифра пропусков. Слепота
неотличима от тишины — ровно то, против чего заводился Этап 0.

Интерфейс выведен из того, что спрашивают на самом деле: скаляр по
запросу, значения по одной метке, значения по нескольким. Всё — через
`Measurement`, который обязывает вызывающего различить незнание и ноль.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Mapping, Sequence, Tuple

from app.providers.measurement import Measurement

__all__ = [
    "MetricsProvider",
    "MetricsProviderError",
    "Measurement",
]


class MetricsProviderError(RuntimeError):
    """Источник метрик не ответил.

    Это НЕ «серий не найдено». Вызывающий, поймав это, не имеет права
    записать ноль или пустоту: состояние метрик неизвестно.
    """


class MetricsProvider(ABC):
    """Источник метрик. Все методы read-only."""

    #: Имя источника — попадает в логи и в записи наблюдений.
    name: str

    @abstractmethod
    async def scalar(self, query: str) -> Measurement[float]:
        """Одно число по запросу.

        `Measurement.of(0.0)` — метрика есть и равна нулю.
        `Measurement.unknown(...)` — источник не ответил.
        """

    @abstractmethod
    async def by_label(
        self, query: str, label: str
    ) -> Measurement[Dict[str, float]]:
        """Значения, разложенные по одной метке (например, `pod`).

        Пустой словарь внутри измерения — законный ответ «серий нет».
        Отказ источника — `unknown`, и это разные вещи: на первом можно
        сделать вывод, на втором нельзя.
        """

    @abstractmethod
    async def by_labels(
        self, query: str, labels: Sequence[str]
    ) -> Measurement[Mapping[Tuple[str, ...], float]]:
        """То же по нескольким меткам: ключ составной.

        Нужно там, где сущность определяется парой: наблюдения ingress
        ключуются `(host, path)`.
        """
