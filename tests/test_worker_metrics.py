"""Экспорт метрик celery-воркера.

Замер 17.09.2026: в VictoriaMetrics не было НИ ОДНОЙ серии `llm_*`, хотя
весь LLM-слой инструментирован, а VMPodScrape уже перечислял
`copilot-worker` в селекторе. Причина: сервер метрик поднимался только в
API-процессе, у воркера не было ни порта, ни сервера — `curl
localhost:8001/metrics` внутри пода возвращал пустоту.

Снаружи это выглядело исправным: код есть, scrape-объект есть. Не было
только того, что скрейпить.
"""
import pytest

from app.observability import worker_metrics


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setattr(worker_metrics, "_started", False)
    yield
    monkeypatch.setattr(worker_metrics, "_started", False)


def test_server_not_started_without_multiproc_dir(monkeypatch):
    """Без каталога сервер НЕ поднимается — и это намеренно.

    Он отдавал бы реестр родительского процесса, где задачи не
    исполняются, то есть ровные нули. Отсутствующий таргет виден как
    отсутствующий, а нули неотличимы от «всё тихо» — ровно та ошибка, за
    которой мы весь день охотимся.
    """
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    assert worker_metrics.start_worker_metrics_server() is False


def test_empty_env_value_counts_as_unset(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", "   ")
    assert worker_metrics.multiproc_dir() is None
    assert worker_metrics.start_worker_metrics_server() is False


def test_stale_files_are_removed(tmp_path, monkeypatch):
    """Файлы прошлого запуска не должны попадать в сумму.

    `MultiProcessCollector` честно суммирует всё, что лежит в каталоге, —
    счётчики «помнили» бы прогоны, которых в этом поде не было. Том
    emptyDir переживает рестарт контейнера в том же поде.
    """
    stale = tmp_path / "counter_12345.db"
    stale.write_bytes(b"leftover")
    keep = tmp_path / "notes.txt"
    keep.write_text("не метрика")

    worker_metrics._prepare_dir(tmp_path)

    assert not stale.exists(), "старый .db обязан быть удалён"
    assert keep.exists(), "посторонние файлы не трогаем"


def test_prepare_dir_creates_missing_path(tmp_path):
    target = tmp_path / "prometheus-multiproc"
    worker_metrics._prepare_dir(target)
    assert target.is_dir()


def test_mark_dead_is_noop_without_env(monkeypatch):
    """Без multiprocess-режима отмечать нечего — и падать тоже не на чем."""
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    worker_metrics.mark_worker_process_dead(4242)


def test_register_signals_is_safe_to_call(monkeypatch):
    """Подписка на сигналы не должна ронять импорт задач."""
    worker_metrics.register_celery_signals()


def test_port_matches_scrape_target():
    """Порт совпадает с тем, куда целится VMPodScrape."""
    import io
    import yaml
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    docs = [
        d for d in yaml.safe_load_all(
            io.open(root / "k8s" / "monitoring.yaml", encoding="utf-8")
        ) if d
    ]
    scrapes = [d for d in docs if d.get("kind") == "VMPodScrape"]
    assert scrapes, "не найден VMPodScrape"
    targets = {
        ep.get("targetPort")
        for d in scrapes for ep in d["spec"].get("podMetricsEndpoints", [])
    }
    assert worker_metrics.WORKER_METRICS_PORT in targets, (
        f"порт {worker_metrics.WORKER_METRICS_PORT} не совпадает с targetPort "
        f"в VMPodScrape ({targets})"
    )
