"""Экспорт prometheus-метрик из celery-воркера.

ЗАЧЕМ. Весь пайплайн разбора инцидента — анализ, гипотезы, критик,
синтез, remediation — исполняется в celery-воркерах. Там же считаются
`llm_tokens_total`, `llm_request_duration_seconds`,
`llm_errors_per_agent_total`, длительности стадий и счётчики фактов.

Но HTTP-сервер метрик поднимался только в `app/main.py`, то есть в
API-процессе. У воркера не было ни порта, ни сервера — `curl
localhost:8001/metrics` внутри пода возвращал пустоту. Замер 17.09.2026:
в VictoriaMetrics НИ ОДНОЙ серии `llm_*`, а `pipeline_*` присутствовали
со счётчиком 2 — это редкие срабатывания в API, не работа воркеров.

Снаружи это выглядело нормально: метрики в коде есть, `VMPodScrape`
существует и даже перечисляет `copilot-worker` в селекторе. Не было
только того, что скрейпить.

Цена: расход токенов, латентность моделей и доля ошибок по агентам не
наблюдались вовсе — то есть ни сравнить две модели, ни поставить
cost budget было нельзя.

MULTIPROCESS. Воркер запускается prefork'ом (`--concurrency=2`), и
задачи исполняются в дочерних процессах. У `prometheus_client` реестр
процесс-локальный, поэтому сервер в родителе отдавал бы нули: счётчики
инкрементятся у детей. Лечится штатным multiprocess-режимом: дети пишут
в общий каталог (`PROMETHEUS_MULTIPROC_DIR`), родитель отдаёт сумму
через `MultiProcessCollector`.

Переменная окружения ОБЯЗАНА быть выставлена до импорта метрик — иначе
`Counter`/`Histogram` создадутся в обычном режиме. Поэтому её задаёт
манифест (`k8s/worker.yaml`), а не этот модуль.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger()

#: Тот же порт, что у API, и то же имя порта в манифесте (`metrics`) —
#: VMPodScrape один на оба пода и целится в `targetPort: 8001`.
WORKER_METRICS_PORT = 8001

_ENV_MULTIPROC_DIR = "PROMETHEUS_MULTIPROC_DIR"

#: Поднят ли сервер. Celery шлёт `celeryd_init` один раз, но при
#: повторной инициализации (тесты, embedded worker) второй bind на тот же
#: порт упал бы OSError и уронил старт воркера.
_started = False


def multiproc_dir() -> Optional[Path]:
    raw = (os.environ.get(_ENV_MULTIPROC_DIR) or "").strip()
    return Path(raw) if raw else None


def _pid_from_name(name: str) -> Optional[int]:
    """`counter_123.db` / `gauge_livesum_123.db` → 123."""
    stem = name.rsplit(".", 1)[0]
    tail = stem.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Процесс есть, но чужой — трогать его файл нельзя тем более.
        return True
    return True


def _prepare_dir(path: Path) -> None:
    """Убрать файлы МЁРТВЫХ процессов, не тронув живые.

    `emptyDir` переживает рестарт контейнера в том же поде, и оставшиеся
    файлы `MultiProcessCollector` честно суммирует — счётчики «помнили» бы
    прогоны, которых в этом контейнере не было.

    Чистить всё подряд НЕЛЬЗЯ. Метрики создаются при импорте модулей, то
    есть ДО celeryd_init: к моменту этого вызова файл текущего процесса уже
    существует, и снести его — значит потерять всё, что он успел записать,
    а дальше писать в удалённый inode.

    Каталог здесь только досоздаётся для локальных запусков и тестов; в
    кластере его создаёт kubelet, монтируя том. Полагаться на mkdir тут
    нельзя по той же причине: первый Counter конструируется раньше.
    """
    path.mkdir(parents=True, exist_ok=True)
    for leftover in path.glob("*.db"):
        pid = _pid_from_name(leftover.name)
        if pid is None or _process_alive(pid):
            continue
        try:
            leftover.unlink()
        except OSError as exc:
            log.warning("worker_metrics.stale_file_not_removed",
                        file=str(leftover), error=str(exc))


def start_worker_metrics_server(port: int = WORKER_METRICS_PORT) -> bool:
    """Поднять /metrics в ГЛАВНОМ процессе воркера. True — если поднят.

    Без `PROMETHEUS_MULTIPROC_DIR` сервер НЕ поднимается намеренно: он
    отдавал бы реестр родителя, где задач не исполняется, то есть ровные
    нули. Пустой ответ честнее — он виден как отсутствующий таргет, а нули
    неотличимы от «всё тихо».
    """
    global _started
    if _started:
        return True

    directory = multiproc_dir()
    if directory is None:
        log.warning(
            "worker_metrics.disabled",
            reason=f"{_ENV_MULTIPROC_DIR} не задан",
            consequence="метрики воркера не экспортируются; нули отдавать не будем",
        )
        return False

    try:
        _prepare_dir(directory)
        from prometheus_client import CollectorRegistry, multiprocess, start_http_server

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        start_http_server(port=port, registry=registry)
    except Exception as exc:  # noqa: BLE001 — старт воркера важнее метрик
        log.error("worker_metrics.start_failed", port=port, error=str(exc))
        return False

    _started = True
    log.info("worker_metrics.started", port=port, multiproc_dir=str(directory))
    return True


def mark_worker_process_dead(pid: int) -> None:
    """Убрать gauge'и завершившегося ребёнка.

    Без этого метрики мёртвого процесса остаются в сумме навсегда: celery
    рециклит воркеров (`--max-tasks-per-child`), и за сутки набежал бы
    десяток фантомных серий.
    """
    if multiproc_dir() is None:
        return
    try:
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(pid)
    except Exception as exc:  # noqa: BLE001
        log.warning("worker_metrics.mark_dead_failed", pid=pid, error=str(exc))


def register_celery_signals() -> None:
    """Подписаться на сигналы celery. Без celery — тихо ничего не делает."""
    try:
        from celery.signals import celeryd_init, worker_process_shutdown
    except ImportError:  # окружение без celery (скрипты, часть тестов)
        return

    @celeryd_init.connect(weak=False)
    def _on_celeryd_init(**_kwargs) -> None:
        start_worker_metrics_server()

    @worker_process_shutdown.connect(weak=False)
    def _on_worker_process_shutdown(pid: Optional[int] = None, **_kwargs) -> None:
        if pid is not None:
            mark_worker_process_dead(pid)
