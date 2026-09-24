"""Именованные проверки исхода для секции `verify` playbook-а v2.

Playbook ссылается на проверку по имени (`verify: [converged, healthy]`), а
не описывает её сам: логика «стало ли лучше» уже живёт в
`verification.assess()` и протестирована там. Здесь — только словарь
«имя → как прочитать ответ из `assess()["checks"]`».

Почему отдельный модуль, а не словарь внутри verification.py: схема playbook-а
валидирует имена на загрузке YAML, и тянуть ради этого kubectl-снимки, БД и
Celery в импорт схемы незачем. Модуль без зависимостей — схема импортирует его
безопасно.

Каждая проверка возвращает True / False / None, где None — «не удалось
проверить» (снимок недоступен, uid до действия неизвестен, алерта нет в
графе). None — честный пробел, а не провал и не успех: ровно как UNKNOWN у
фактов.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, Mapping, Optional

CheckFn = Callable[[Mapping[str, Any]], Optional[bool]]


def _flag(key: str) -> CheckFn:
    """Проверка, которая просто читает булев флаг из checks."""
    def check(checks: Mapping[str, Any]) -> Optional[bool]:
        value = checks.get(key)
        return value if isinstance(value, bool) else None
    return check


def _no_new_crash_events(checks: Mapping[str, Any]) -> Optional[bool]:
    # assess() кладёт счётчик, а не флаг: 0 — «новых крашей нет».
    count = checks.get("new_crash_events")
    if isinstance(count, bool) or not isinstance(count, int):
        return None
    return count == 0


#: Имена — ключи `assess()["checks"]`, кроме `no_new_crash_events` (там
#: счётчик). Тест сверяет словарь с живым выводом assess(): переименование
#: ключа там роняет тест, а не тихо превращает проверку в вечный None.
VERIFY_CHECKS: Dict[str, CheckFn] = {
    "same_identity": _flag("same_identity"),
    "action_took_effect": _flag("action_took_effect"),
    "converged": _flag("converged"),
    "healthy": _flag("healthy"),
    "alert_resolved": _flag("alert_resolved"),
    "no_new_crash_events": _no_new_crash_events,
}


def evaluate_verify(
    names: Iterable[str], checks: Mapping[str, Any],
) -> Dict[str, Optional[bool]]:
    """Прогнать перечисленные проверки по выводу assess().

    Неизвестное имя — KeyError: схема не пустит такое имя в YAML, и если
    оно всё-таки дошло сюда, молча вернуть None значило бы скрыть дрейф.
    """
    return {name: VERIFY_CHECKS[name](checks) for name in names}
