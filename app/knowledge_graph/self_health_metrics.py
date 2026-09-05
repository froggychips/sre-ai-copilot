"""Self-health в метриках, а не только в audit-логе и Discord.

Проверки существуют с rc.13 и умеют находить ровно те поломки, ради которых
их писали. Видно их при этом было в трёх местах: audit-log, embed в Discord
при `fail` и ручной запуск в поде. Метрик нет, значит нет ни алерта, ни
дашборда, ни истории — только событие, которое надо не пропустить.

Замер 05.09.2026: на `:8001` 31 метрика, из них своя одна
(`celery_queue_length`); VMRule про копилот в кластере ноль, а
`k8s/prometheus-rules.yaml` с одиннадцатью правилами не применён. За тот же
день нашлись: мёртвый пятнадцать суток источник statics, 96 недоставок
алертов в сутки и чистка графа, заблокированная собственным порогом. Ни одно
из трёх не всплыло само — потому что всплывать было негде.

**Почему через Redis, а не напрямую.** Проверки считает celery-задача
(`kg_self_health_check`), а `/metrics` отдаёт api-процесс: у них разные
поды. Задача кладёт снимок в Redis, экспортёр читает его на скрейпе — один
GET раз в 30 секунд. Прямой путь потребовал бы поднимать сервер метрик в
worker'е и разбираться с prometheus_client в форках celery.

**Почему Collector, а не Gauge.set().** Gauge в api-процессе никто не
обновляет — данные приходят из чужого пода. Collector читает актуальный
снимок в момент скрейпа, поэтому метрика не «залипает» на последнем
значении, если задача перестала ходить: `last_run_timestamp` покажет, когда
снимок сняли, и алерт по нему поймает остановку самих проверок.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Optional

import structlog
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import REGISTRY, Collector

log = structlog.get_logger(__name__)

#: Ключ снимка. Соседи по неймингу — `stats:beat:last_run:<task>`.
SNAPSHOT_KEY = "stats:self_health:last"

#: Сколько снимок считается живым. Проверки ходят каждые 30 минут; сутки —
#: заведомо больше любого разумного отставания и достаточно мало, чтобы
#: протухший снимок не выдавал себя за свежий после недельного простоя.
SNAPSHOT_TTL_SECONDS = 24 * 3600

#: Статус проверки числом: с ним работают и `>= 1` (что-то не так), и
#: `== 2` (сломано). Строковая метка вместо значения потребовала бы
#: сравнивать лейблы в каждом правиле.
_STATUS_VALUES = {"ok": 0.0, "warn": 1.0, "fail": 2.0}
_UNKNOWN_STATUS = 3.0


def _redis():
    """Тот же клиент, что у beat-heartbeat. None — Redis недоступен.

    Переиспользуем `digest.state._get_beat_redis`: он уже кэширует
    соединение и настроен на `decode_responses`, а заводить третий клиент
    ради одного ключа не за чем.
    """
    try:
        from app.services.digest.state import _get_beat_redis
        return _get_beat_redis()
    except Exception as e:  # noqa: BLE001 — экспортёр не имеет права падать
        log.warning("self_health_metrics.redis_unavailable", error=str(e))
        return None


def publish_snapshot(overall: str, results: Iterable[Any], now_ts: float) -> bool:
    """Сохранить снимок проверок для экспортёра. True если записали.

    Вызывается из `kg_self_health_check` после прогона. Ошибка записи не
    должна валить задачу: метрики — следствие проверок, а не наоборот.
    """
    client = _redis()
    if client is None:
        return False
    payload = {
        "overall": overall,
        "ts": now_ts,
        "checks": [
            {"name": r.name, "status": r.status} for r in results
        ],
    }
    try:
        client.set(SNAPSHOT_KEY, json.dumps(payload), ex=SNAPSHOT_TTL_SECONDS)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("self_health_metrics.publish_failed", error=str(e))
        return False


def read_snapshot() -> Optional[Dict[str, Any]]:
    """Последний снимок или None (Redis недоступен / ключа нет / битый JSON)."""
    client = _redis()
    if client is None:
        return None
    try:
        raw = client.get(SNAPSHOT_KEY)
    except Exception as e:  # noqa: BLE001
        log.warning("self_health_metrics.read_failed", error=str(e))
        return None
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        log.warning("self_health_metrics.snapshot_malformed")
        return None
    return data if isinstance(data, dict) else None


class SelfHealthCollector(Collector):
    """Отдаёт последний снимок self-health как метрики.

    Три семейства:

      * `copilot_self_health_check_status{check}` — 0 ok / 1 warn / 2 fail /
        3 неизвестный статус. Числом, чтобы правило писалось как `>= 2`, а не
        сравнением лейбла;
      * `copilot_self_health_status` — агрегат, тем же кодом;
      * `copilot_self_health_last_run_timestamp` — unixtime снимка. По нему
        ловится остановка САМИХ проверок: без него мёртвая задача выглядит
        как вечное `ok`.

    Снимка нет вовсе — не отдаём ничего. Пустой ряд честнее нуля: `0` здесь
    означает «проверено, всё хорошо», и выдавать его за «не знаю» — ровно та
    подмена, из-за которой мёртвый источник statics пятнадцать суток
    считался здоровым.
    """

    def collect(self):  # noqa: D102 — интерфейс prometheus_client
        snap = read_snapshot()
        if not snap:
            return

        per_check = GaugeMetricFamily(
            "copilot_self_health_check_status",
            "Статус проверки self-health: 0 ok, 1 warn, 2 fail, 3 unknown",
            labels=["check"],
        )
        for item in snap.get("checks") or []:
            name = str(item.get("name") or "")
            if not name:
                continue
            per_check.add_metric(
                [name],
                _STATUS_VALUES.get(str(item.get("status")), _UNKNOWN_STATUS),
            )
        yield per_check

        yield GaugeMetricFamily(
            "copilot_self_health_status",
            "Агрегат self-health: 0 ok, 1 warn, 2 fail, 3 unknown",
            value=_STATUS_VALUES.get(str(snap.get("overall")), _UNKNOWN_STATUS),
        )

        ts = snap.get("ts")
        if isinstance(ts, (int, float)):
            yield GaugeMetricFamily(
                "copilot_self_health_last_run_timestamp",
                "Unixtime последнего прогона self-health",
                value=float(ts),
            )


_registered = False


def register_collector(registry=REGISTRY) -> bool:
    """Подключить экспортёр. Идемпотентно — повторный вызов ничего не делает.

    Двойная регистрация в prometheus_client — исключение, а не no-op, и
    падать на ней при повторном импорте модуля не за что.
    """
    global _registered
    if _registered:
        return False
    try:
        registry.register(SelfHealthCollector())
    except ValueError:  # уже зарегистрирован в этом реестре
        _registered = True
        return False
    _registered = True
    return True
