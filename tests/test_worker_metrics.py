"""Экспорт метрик celery-воркера.

Замер 17.09.2026: в VictoriaMetrics не было НИ ОДНОЙ серии `llm_*`, хотя
весь LLM-слой инструментирован, а VMPodScrape уже перечислял
`copilot-worker` в селекторе. Причина: сервер метрик поднимался только в
API-процессе, у воркера не было ни порта, ни сервера — `curl
localhost:8001/metrics` внутри пода возвращал пустоту.

Снаружи это выглядело исправным: код есть, scrape-объект есть. Не было
только того, что скрейпить.
"""
import os

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


def test_dead_process_files_are_removed(tmp_path):
    """Файлы прошлого запуска не должны попадать в сумму.

    `MultiProcessCollector` честно суммирует всё, что лежит в каталоге, —
    счётчики «помнили» бы прогоны, которых в этом контейнере не было. Том
    emptyDir переживает рестарт контейнера в том же поде.
    """
    # PID, которого заведомо нет: max_pid на linux ≤ 4194304.
    stale = tmp_path / "counter_4194305.db"
    stale.write_bytes(b"leftover")
    keep = tmp_path / "notes.txt"
    keep.write_text("не метрика")

    worker_metrics._prepare_dir(tmp_path)

    assert not stale.exists(), "файл мёртвого процесса обязан быть удалён"
    assert keep.exists(), "посторонние файлы не трогаем"


def test_live_process_file_survives(tmp_path):
    """Файл ЖИВОГО процесса не трогаем — иначе теряем свои же метрики.

    Метрики создаются при импорте модулей, то есть до celeryd_init: к
    моменту очистки файл текущего процесса уже существует. Снести его —
    значит потерять всё записанное и продолжить писать в удалённый inode.
    """
    mine = tmp_path / f"counter_{os.getpid()}.db"
    mine.write_bytes(b"my metrics")

    worker_metrics._prepare_dir(tmp_path)

    assert mine.exists(), "файл живого процесса удалять нельзя"


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


def test_helm_worker_matches_raw_manifest():
    """Helm-чарт настроен так же, как k8s/worker.yaml.

    Эта пара уже разъезжалась: RBAC для CNPG добавили в base, а в чарте
    остались одни deployments, и helm-развёртывание молча теряло половину
    топологии. Здесь цена расхождения такая же — метрики воркера просто не
    экспортировались бы, и это не видно ниоткуда, кроме пустого графика.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    chart = (root / "helm" / "sre-ai-copilot" / "templates"
             / "deployment-worker.yaml").read_text(encoding="utf-8")

    assert "PROMETHEUS_MULTIPROC_DIR" in chart, (
        "в чарте нет PROMETHEUS_MULTIPROC_DIR — start_worker_metrics_server "
        "вернёт False на каждой helm-установке"
    )
    assert re.search(r"containerPort:\s*8001", chart), (
        "в чарте не объявлен порт метрик"
    )
    assert "prometheus-multiproc" in chart, (
        "каталог должен монтироваться томом: без него первый Counter падает "
        "FileNotFoundError ещё на импорте"
    )

    raw = (root / "k8s" / "worker.yaml").read_text(encoding="utf-8")
    raw_path = re.search(
        r"name: PROMETHEUS_MULTIPROC_DIR\s*\n\s*value:\s*(\S+)", raw
    )
    chart_path = re.search(
        r"name: PROMETHEUS_MULTIPROC_DIR\s*\n\s*value:\s*(\S+)", chart
    )
    assert raw_path and chart_path, "не разобрал значение переменной"
    assert raw_path.group(1) == chart_path.group(1), (
        f"пути расходятся: raw={raw_path.group(1)} chart={chart_path.group(1)}"
    )
