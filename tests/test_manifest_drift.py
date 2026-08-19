"""Расхождение между репозиторием и кластером должно быть видимым.

За один день 19.08.2026 нашлось три случая, и ни один не виден из кода:

  * `CronJob/postgres-backup` описан и никогда не применялся — база 5.5 ГБ
    в одной реплике не бэкапилась вообще;
  * `Deployment/jaeger` и `Service/jaeger` описаны и отсутствуют;
  * `--concurrency` воркера: 2 в манифесте, 4 в кластере. Манифест правили
    вместе с арифметикой памяти, но не применили — воркеры продолжали падать.

Полноценный GitOps (Flux/ArgoCD) решал бы это лучше, но ставит в кластер
контроллер с правом изменять ресурсы, а кластер здесь общий с боевым игровым
бэкендом. Такое решение принимает команда. Эта проверка даёт главное
свойство — расхождение перестаёт быть невидимым — только на чтении.
"""
import pathlib
import subprocess
import sys

import pytest

SCRIPT = pathlib.Path(__file__).parent.parent / "scripts" / "manifest_drift.py"


@pytest.fixture(scope="module")
def module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("manifest_drift", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_exists_and_runs():
    """Проверка бесполезна, если её нельзя запустить одной командой."""
    res = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0


def test_manifests_are_discovered(module):
    """Если разбор молча вернёт пусто, проверка станет вечно зелёной."""
    found = module._manifests()
    assert len(found) > 10, "манифесты не читаются из k8s/"
    kinds = {k for k, _, _ in found}
    assert "Deployment" in kinds and "CronJob" in kinds


def test_secrets_are_not_tracked(module):
    """Секреты намеренно не хранятся в git — их отсутствие не расхождение."""
    names = {name for _, name, _ in module._manifests()}
    assert "sre-ai-secrets" not in names


def test_cluster_scoped_kinds_are_known(module):
    """ClusterRole ищется без -n; спутав scope, получим ложное «отсутствует»."""
    assert "ClusterRole" in module.CLUSTER_SCOPED
    assert "Deployment" not in module.CLUSTER_SCOPED


def test_unavailable_kubectl_does_not_report_everything_missing(module, monkeypatch):
    """Недоступный kubectl — не повод объявить весь кластер пустым.

    Иначе первая же сетевая заминка дала бы отчёт «отсутствует всё», и
    проверке перестали бы верить.
    """
    def boom(*_a, **_kw):
        raise OSError("kubectl нет в PATH")

    monkeypatch.setattr(module.subprocess, "run", boom)
    assert module._exists("Deployment", "whatever", "sre-ai") is True


def test_exit_code_reflects_drift(module, monkeypatch):
    """Ненулевой код возврата — чтобы проверку можно было поставить в CI."""
    monkeypatch.setattr(module, "check",
                        lambda ns: {"namespace": ns, "checked": 5,
                                    "missing": [{"kind": "CronJob",
                                                 "name": "x", "file": "y.yaml"}]})
    monkeypatch.setattr(sys, "argv", ["manifest_drift.py"])
    assert module.main() == 1


def test_no_drift_gives_zero(module, monkeypatch):
    monkeypatch.setattr(module, "check",
                        lambda ns: {"namespace": ns, "checked": 5, "missing": []})
    monkeypatch.setattr(sys, "argv", ["manifest_drift.py"])
    assert module.main() == 0
