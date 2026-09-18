"""`Measurement` — значение ИЛИ явное «не измеряли».

Тип вынесен из `logs.py`, когда за логами пришли метрики: вопрос «данных
нет или показатель равен нулю» стоит одинаково перед любым источником, и
двум провайдерам незачем отвечать на него по-разному.

Почему типом, а не соглашением вроде «-1 значит неизвестно» или «пустой
словарь значит отказ»: соглашение забывается на второй реализации, а тип
приходится обработать. Цена ошибки видна в том же репозитории —
`VMClient.query_instant` честно различает None и 0.0 (и объясняет это в
докстринге), а соседние `query_instant_by` и `query_instant_by_labels`
отдают при отказе пустой словарь, неотличимый от «у сервисов нет метрик».
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Optional, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Measurement(Generic[T]):
    """Значение ИЛИ явное «не измеряли».

    `measured=False` означает, что окно не наблюдалось: источник не
    ответил, не настроен или отказал. `value` при этом None — не 0, не
    пустой список, ничего, что можно спутать с ответом.

    `exact=False` — значение измерено, но является НИЖНЕЙ оценкой: так
    отвечает Seq, когда окно упирается в потолок пагинации. Отличать это
    от точного счёта важно ровно там, где по счёту принимают решение:
    «не меньше 20000» и «ровно 20000» — разные утверждения.
    """

    value: Optional[T]
    measured: bool
    exact: bool = True
    reason: str = ""

    @classmethod
    def of(cls, value: T, *, exact: bool = True) -> "Measurement[T]":
        return cls(value=value, measured=True, exact=exact)

    @classmethod
    def unknown(cls, reason: str) -> "Measurement[T]":
        return cls(value=None, measured=False, reason=reason)

    def or_else(self, fallback: T) -> T:
        """Значение или запасное. Вызывать осознанно.

        Существует для мест, где подстановка действительно уместна
        (например, показать 0 в тексте для человека, который видит рядом
        пометку о недоступности). В логике решений вместо этого нужно
        смотреть на `measured`.
        """
        return self.value if self.measured and self.value is not None else fallback
