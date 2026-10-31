from typing import Iterable, Optional, Tuple

import structlog
from prometheus_client import Counter, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import REGISTRY, Collector

log = structlog.get_logger(__name__)

REQUEST_LATENCY = Histogram(
    "request_duration_seconds",
    "HTTP latency",
    buckets=(0.1, 0.5, 1.0, 5.0, float("inf")),
)

# A3: счётчик отфильтрованных alerts на входе webhook handler-а.
# `reason` — категория фильтра:
#   "allowlist"        — alertname matched ALERT_SUPPRESS_NAMES (A3)
#   "inhibited_warn"   — AM inhibit + не critical → skip Discord (A1)
#   "inhibited_info"   — AM inhibit + severity=info (всегда skip)
# Labels отдельно по alertname — кардинальность ограничена (allowlist
# короткий, AM-suppressed events редкие). Метрика scrape-ится /metrics.
ALERTS_SUPPRESSED = Counter(
    "alerts_suppressed_total",
    "Alerts filtered out before enrich/Discord (allowlist or AM-inhibit gate)",
    ["reason", "alertname"],
)


# Алерты, которые AM не доставил из-за `max_alerts` у receiver-а
# (`truncatedAlerts` webhook-payload-а). Растёт — группа шире лимита, и
# часть её copilot не видит вовсе: ни в KG, ни в Discord. `endpoint` —
# какой из /webhooks/alertmanager* принял batch; `receiver` — имя из AM
# (их три, кардинальность ограничена).
ALERTS_TRUNCATED = Counter(
    "alertmanager_alerts_truncated_total",
    "Alerts dropped by Alertmanager max_alerts before reaching the webhook",
    ["endpoint", "receiver"],
)


def observe_request_latency(duration):
    REQUEST_LATENCY.observe(duration)


# ── celery_queue_length ─────────────────────────────────────────────────────
#
# До 31.10.2026 здесь жил `Gauge("celery_queue_length")` и функция
# `set_queue_length`, которую не вызывал никто: метрика экспортировалась
# ровным нулём, а правило `CopilotCeleryBacklog` в k8s/monitoring.yaml
# стояло на нём с 05.09.2026 и не могло сработать в принципе. Ноль
# неотличим от «всё тихо» — тот же класс дефекта, что мёртвый источник
# statics, пятнадцать суток считавшийся здоровым.
#
# Теперь это Collector: длина очередей читается из брокера В МОМЕНТ скрейпа
# (`LLEN <queue>` — у kombu-транспорта Redis очередь и есть список с именем
# очереди), по одной серии на очередь. Лейбл `queue` обязателен с
# появлением очереди `sync` (copilot-worker-sync): сумма по всем очередям
# скрыла бы, какая именно встала, а правило считает `max by (queue)`.
#
# Экспортирует api-процесс (регистрация в app/main.py рядом с self-health):
# у воркера реестр multiprocess, и он отдавал бы те же числа второй раз.
# Redis недоступен — серий нет вовсе: пустой ряд честнее нуля.

_UNSET: Tuple[str, ...] = ()


def _redis():
    """Тот же sync-клиент, что у beat-heartbeat и self-health. None — нет Redis."""
    try:
        from app.services.digest.state import _get_beat_redis
        return _get_beat_redis()
    except Exception as e:  # noqa: BLE001 — экспортёр не имеет права падать
        log.warning("celery_queue_metrics.redis_unavailable", error=str(e))
        return None


def celery_queue_names() -> Tuple[str, ...]:
    """Очереди, за которыми следим: default, sync, executor (без дублей)."""
    from app.config import settings
    from app.workers.tasks import celery_app

    names = (
        str(celery_app.conf.task_default_queue or "celery"),
        settings.SYNC_QUEUE_NAME,
        settings.EXECUTOR_QUEUE_NAME,
    )
    seen: list = []
    for name in names:
        if name and name not in seen:
            seen.append(name)
    return tuple(seen)


def read_queue_lengths(
    client=None, queues: Optional[Iterable[str]] = None,
) -> Optional[dict]:
    """{queue: pending} по брокеру или None, если Redis недоступен.

    Одна недоступная очередь роняет весь снимок: отдать «celery=0» при
    упавшем LLEN — та же ложь, от которой уходим.
    """
    client = client if client is not None else _redis()
    if client is None:
        return None
    out = {}
    try:
        for name in (queues if queues is not None else celery_queue_names()):
            out[name] = int(client.llen(name))
    except Exception as e:  # noqa: BLE001
        log.warning("celery_queue_metrics.read_failed", error=str(e))
        return None
    return out


class CeleryQueueLengthCollector(Collector):
    """`celery_queue_length{queue}` — задачи, ждущие воркера, по очередям.

    Считаются только ожидающие в списке брокера. Зарезервированные
    воркером, но не подтверждённые (acks_late → `unacked`) сюда не входят:
    это уже исполняющиеся задачи, а правило про бэклог спрашивает, успевают
    ли воркеры разбирать очередь.
    """

    def collect(self):  # noqa: D102 — интерфейс prometheus_client
        lengths = read_queue_lengths()
        if lengths is None:
            return
        fam = GaugeMetricFamily(
            "celery_queue_length",
            "Pending Celery tasks per broker queue",
            labels=["queue"],
        )
        for name, pending in lengths.items():
            fam.add_metric([name], float(pending))
        yield fam


_registered = False


def register_queue_length_collector(registry=REGISTRY) -> bool:
    """Подключить экспортёр очередей. Идемпотентно."""
    global _registered
    if _registered:
        return False
    try:
        registry.register(CeleryQueueLengthCollector())
    except ValueError:  # уже зарегистрирован в этом реестре
        _registered = True
        return False
    _registered = True
    return True
