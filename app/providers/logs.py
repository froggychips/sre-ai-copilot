"""`LogProvider` — интерфейс к логам, независимый от того, где они лежат.

Копайлот спрашивает у логов три вещи: сколько ошибок у сервиса за окно,
какие сообщения самые частые, дай образец. Это и есть интерфейс — ровно
то, что умеет `app/context/seq_client.py`, и ровно то, что сможет ответить
адаптер поверх ClickHouse.

ГЛАВНОЕ ТРЕБОВАНИЕ: «НЕТ ДАННЫХ» ≠ «НОЛЬ ОШИБОК»
-------------------------------------------------
Провайдер обязан уметь сказать «за это окно я ничего не знаю» отдельно от
«за это окно ошибок не было». Если интерфейс их не различает, молчание
источника становится неотличимо от здоровья сервиса — и зашивается в
фундамент, поверх которого потом строится всё остальное.

Требование не теоретическое: `SeqClient.count_events` до сих пор
возвращает `0` при любой ошибке запроса, хотя рядом в том же файле
объявлен `SeqQueryError` с докстрингом о том, что это разные вещи. Ноль,
означающий «Seq не ответил», уходил дальше как «ошибок нет».

Поэтому измеримость выражена в типе — `Measurement`, — а не соглашением о
том, что «-1 значит неизвестно». Соглашение забывается на второй
реализации; тип приходится обработать.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Mapping, Optional, Sequence

from app.providers.measurement import Measurement

__all__ = [
    "LogProvider",
    "LogProviderError",
    "Measurement",
    "ServiceLogStats",
]


class LogProviderError(RuntimeError):
    """Источник логов не ответил.

    Это НЕ «событий не найдено». Вызывающий, поймав это, не имеет права
    записать ноль: состояние логов неизвестно.
    """


@dataclass(frozen=True)
class ServiceLogStats:
    """Агрегат логов одного сервиса за окно."""

    #: Имя приложения как его называет источник. None — событие не несёт
    #: атрибуции; это не ошибка, а нередкий случай, и терять такие события
    #: нельзя (они попадают в KG со service_id=NULL).
    service: Optional[str]
    count: int
    #: Самый частый шаблон сообщения за окно. Пустая строка — шаблона нет.
    top_message: str = ""
    #: Шаблон → сколько раз встретился. Нужен потребителю, который считает
    #: не только верхушку (например, хэш для дедупликации).
    message_counts: Mapping[str, int] = field(default_factory=dict)


class LogProvider(ABC):
    """Источник логов. Все методы read-only.

    Реализация конструируется под конкретный инстанс (prod / preprod /
    отдельный хост) — у `name` ровно эта роль: попасть в логи и в
    `kg_log_observations.source`, чтобы потом было видно, чьё окно молчало.
    """

    #: Имя инстанса — попадает в записи наблюдений и в логи.
    name: str

    @abstractmethod
    async def count_events(
        self, level: str, since: datetime, until: datetime
    ) -> Measurement[int]:
        """Сколько событий уровня `level` за окно [since, until].

        Возвращает `Measurement.unknown(...)`, если источник не ответил.
        Возврат `Measurement.of(0)` означает именно «событий не было».
        """

    @abstractmethod
    async def service_stats(
        self, level: str, since: datetime, until: datetime, limit: int = 500
    ) -> Measurement[Dict[Optional[str], ServiceLogStats]]:
        """Агрегат по сервисам: сколько и что чаще всего.

        Наружу отдаётся именно агрегат, а не список событий: сырое событие
        — деталь конкретного источника, и требовать его от всех реализаций
        значит пускать форму Seq в интерфейс.
        """

    @abstractmethod
    async def sample_messages(
        self, level: str, since: datetime, until: datetime, limit: int = 10
    ) -> Measurement[Sequence[str]]:
        """Образцы сообщений — для человека, читающего отчёт."""
