"""Контракт ответа источника: EMPTY ≠ SUCCESS.

До 05.09.2026 у задач-источников было два исхода, которые видел надзор:
Celery SUCCESS и Celery FAILURE. Всё остальное — «ходил, но ничего не
получил», «ответила половина инстансов», «источник выключен флагом» —
сливалось в SUCCESS, потому что задача завершилась без исключения. Heartbeat
писался, `check_sync_lag` показывал `ok`.

Так `kg_statics_versions_sync` пятнадцать суток отдавал `observed=0`, так
`kg_seq_logs_sync` 20.08.2026 двенадцать часов отдавал `rows=0` — оба
«успешно». Фикс #350 закрыл это для одной задачи. Здесь — то же правило для
всех: задача-источник обязана сказать, ЧТО означает её результат.

Шесть статусов и что за ними стоит:

  * SUCCESS     — источник ответил, данные есть;
  * PARTIAL     — ответил не весь (3 инстанса Seq из 8; VM отдала серии, но
                  часть запросов упала). Данные есть, но неполные;
  * EMPTY       — источник ответил, объектов ноль. Это не ошибка, но и не
                  подтверждение здоровья: `kubectl get deploy -A` без единого
                  Deployment'а на кластере из 4360 — событие, а не тишина;
  * UNAVAILABLE — до источника не дошли: таймаут, выключен флагом, нет
                  конфигурации. Ответа нет, судить нечего;
  * FAILED      — исключение по дороге;
  * INVALID     — ответ пришёл, но разобрать его нельзя.

Правило для heartbeat одно: он пишется только на SUCCESS и PARTIAL.
Всё остальное — не подтверждение того, что источник жив, а значит и не
повод говорить надзору «свежий прогон был».

**Почему EMPTY не даёт heartbeat.** Соблазн считать «ответил пустотой» за
здоровье велик: формально соединение было. Но именно так выглядит источник,
которого отрезала NetworkPolicy с таймаутом внутри клиента, или API, у
которого сменился формат ответа. «В мире ничего нет» — утверждение, которое
задача-источник почти никогда не может проверить сама; честнее отдать EMPTY
и дать надзору решить, сколько пустых прогонов подряд ещё нормально.

**Для задач, у которых ноль записей — норма** (детектор аномалий без
аномалий, resolve-sync без зависших алертов), статус описывает ответ
ИСТОЧНИКА, а не число вставленных строк: если детектор получил данные для
анализа и не нашёл аномалий — это SUCCESS с `inserted=0`.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Iterable, Mapping, Optional

__all__ = [
    "SourceStatus", "SOURCE_STATUS_KEY", "HEARTBEAT_STATUSES",
    "mark", "status_of", "status_from_counts",
]

#: Ключ в retval задачи. Строка, а не enum-объект: retval уезжает в
#: result backend Celery как JSON, и там enum не переживёт сериализацию.
SOURCE_STATUS_KEY = "source_status"


class SourceStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    INVALID = "invalid"


#: Статусы, при которых прогон считается подтверждением, что источник жив.
HEARTBEAT_STATUSES = frozenset({SourceStatus.SUCCESS, SourceStatus.PARTIAL})


def mark(result: Optional[Dict[str, Any]], status: SourceStatus) -> Dict[str, Any]:
    """Проставить статус в retval. Мутирует и возвращает тот же dict."""
    if not isinstance(result, dict):
        result = {"result": result}
    result[SOURCE_STATUS_KEY] = status.value
    return result


def status_of(result: Any) -> Optional[SourceStatus]:
    """Статус из retval или None, если задача контракт не соблюдает."""
    if not isinstance(result, Mapping):
        return None
    raw = result.get(SOURCE_STATUS_KEY)
    if raw is None:
        return None
    try:
        return SourceStatus(raw)
    except ValueError:
        return SourceStatus.INVALID


def status_from_counts(
    result: Any,
    observed_keys: Iterable[str],
    *,
    unavailable_keys: Iterable[str] = ("skipped",),
    errors_key: Optional[str] = "errors",
) -> SourceStatus:
    """Вывести статус из счётчиков в результате синка.

    Общий случай для задач, чей результат — dict со счётчиками:

      * `error` в результате                       → FAILED;
      * любой из `unavailable_keys` непустой        → UNAVAILABLE
        (источник выключен флагом / не настроен / kubectl не ответил);
      * сумма по `observed_keys` равна нулю        → EMPTY;
      * `errors_key` > 0 при ненулевых данных      → PARTIAL;
      * иначе                                       → SUCCESS.

    `observed_keys` — счётчики того, что источник ОТДАЛ (fetched, seen,
    reached), а не того, что мы записали (inserted): вставок может не быть и
    при живом источнике (дедуп, нет изменений), а вот отсутствие отданного —
    это уже про источник.
    """
    if not isinstance(result, Mapping):
        return SourceStatus.INVALID
    if result.get("error") is not None or result.get("status") == "error":
        return SourceStatus.FAILED
    for key in unavailable_keys:
        if _is_flag(result.get(key)):
            return SourceStatus.UNAVAILABLE
    observed = sum(_count(v) for v in _find_all(result, tuple(observed_keys)))
    if observed <= 0:
        return SourceStatus.EMPTY
    if errors_key and any(_count(v) for v in _find_all(result, (errors_key,))):
        return SourceStatus.PARTIAL
    return SourceStatus.SUCCESS


def _is_flag(value: Any) -> bool:
    """Маркер недоступности — строка-причина или True, но НЕ счётчик.

    У `k8s_pod_events_sync` в результате лежит `"skipped": 0` — сколько
    событий пропущено. Число здесь — статистика, а не «источник выключен»;
    иначе пять пропущенных дубликатов делали бы задачу UNAVAILABLE.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return bool(value)
    return False


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    return 0


def _find_all(result: Mapping[str, Any], keys: tuple) -> Iterable[Any]:
    """Значения ключей на любом уровне вложенности.

    Результаты составных синков вложены по секциям:
    `kg_jobs_sync` → `{'cronjobs': {'cronjobs_fetched': 26, …}, 'jobs': {…}}`,
    `kg_storage_sync` → `{'pvs': {'pvs_fetched': 1211, …}, 'pvcs': {…}}`,
    `kg_topology_resources_sync` → `{'services': {'services_fetched': …}}`.
    Плоский поиск объявил бы их EMPTY после первого же деплоя — и три
    источника потеряли бы heartbeat ровно из-за проверки, которая должна
    была его защищать. Сверено с живыми результатами 05.09.2026.
    """
    for k, v in result.items():
        if k in keys:
            yield v
        elif isinstance(v, Mapping):
            yield from _find_all(v, keys)
