"""k8s_ingress_sync: kubectl-сбой деградирует, а не роняет тик.

Ревью 2026-08-10: `_kubectl_get_ingresses_all` был ЕДИНСТВЕННЫМ
kubectl-хелпером синков без try/except вокруг `subprocess.run(timeout=30)`.
Последствия таймаута (тупящий apiserver / мигнувший VPN):

  * `sync_all_ingresses` умирал трейсбеком, НЕ доходя до
    `record_source_run` — edge-decay guard оставался без отчёта источника,
    и тик терялся целиком, вместо того чтобы честно отчитаться «fetch упал»;
  * `ingress_observations_sync` импортирует тот же хелпер — его тик умирал
    заодно.

Соседи (`k8s_jobs_sync`, `k8s_topology_resources_sync`) в этом месте ловят
TimeoutExpired и возвращают пустой список. Тесты фиксируют оба контракта:
strict-вариант сигналит ошибку (→ errors++), нестрогий отдаёт [].
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.knowledge_graph.k8s_ingress_sync as mod
from app.database import Base
from app.knowledge_graph.edge_decay_guard import (
    SOURCE_INGRESS_SYNC, get_source_report)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_kubectl_timeout_degrades_instead_of_traceback(db):
    """TimeoutExpired → stats с errors=1, без исключения наружу."""
    with patch.object(
        mod.subprocess, "run",
        side_effect=subprocess.TimeoutExpired(cmd="kubectl", timeout=30),
    ):
        stats = mod.sync_all_ingresses(db)

    assert stats["ingresses_fetched"] == 0
    assert stats["routes_seen"] == 0
    # Именно ошибка, а не «в кластере 0 ingress-ов»: иначе edge-decay guard
    # счёл бы источник здоровым и легализовал удаление ingress-рёбер.
    assert stats["errors"] == 1


def test_kubectl_timeout_still_reports_to_edge_decay_guard(db):
    """Тик не теряется: record_source_run вызывается и на сбое fetch-а."""
    with patch.object(
        mod.subprocess, "run",
        side_effect=subprocess.TimeoutExpired(cmd="kubectl", timeout=30),
    ):
        mod.sync_all_ingresses(db)

    report = get_source_report(SOURCE_INGRESS_SYNC)
    assert report is not None, "отчёт источника потерян — guard ослеп"
    assert report.errors == 1
    assert report.fetched == 0


def test_kubectl_nonzero_rc_counted_as_error(db):
    """rc!=0 (нет прав / apiserver 500) — тот же класс деградации."""
    with patch.object(
        mod.subprocess, "run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="forbidden",
        ),
    ):
        stats = mod.sync_all_ingresses(db)
    assert stats["errors"] == 1
    assert stats["ingresses_fetched"] == 0


def test_bad_json_counted_as_error(db):
    """Обрезанный JSON — тоже сбой fetch-а, не пустой кластер."""
    with patch.object(
        mod.subprocess, "run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"items": [', stderr="",
        ),
    ):
        stats = mod.sync_all_ingresses(db)
    assert stats["errors"] == 1


def test_helper_default_contract_returns_empty_list():
    """Нестрогий вызов (его делает ingress_observations_sync) отдаёт [].

    Тот модуль ждёт именно список и сам логирует `no_ingresses`; исключение
    оттуда унесло бы его тик.
    """
    for effect in (
        subprocess.TimeoutExpired(cmd="kubectl", timeout=30),
        OSError("kubectl not found"),
    ):
        with patch.object(mod.subprocess, "run", side_effect=effect):
            assert mod._kubectl_get_ingresses_all() == []

    with patch.object(
        mod.subprocess, "run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="boom",
        ),
    ):
        assert mod._kubectl_get_ingresses_all() == []


def test_helper_strict_raises_ingress_fetch_error():
    """strict=True — сигнал вызывающему, что fetch упал."""
    with patch.object(
        mod.subprocess, "run",
        side_effect=subprocess.TimeoutExpired(cmd="kubectl", timeout=30),
    ):
        with pytest.raises(mod.IngressFetchError):
            mod._kubectl_get_ingresses_all(strict=True)


def test_healthy_fetch_still_parses_items(db):
    """Happy-path не сломан: items прокидываются как раньше."""
    payload = '{"items": [{"metadata": {"name": "ing", "namespace": "ns"}}]}'
    with patch.object(
        mod.subprocess, "run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout=payload, stderr="",
        ),
    ):
        stats = mod.sync_all_ingresses(db)
    assert stats["ingresses_fetched"] == 1
    assert stats["errors"] == 0
