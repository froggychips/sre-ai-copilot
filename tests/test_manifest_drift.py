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


# ── обратные проверки (18.11.2026): «живёт, но не описано» и «описано иначе» ──


def test_live_only_objects_are_reported(module):
    """ClusterRole sre-ai-events-reader полгода жила только в кластере."""
    described = [("Deployment", "sre-ai-api", "deployment.yaml")]
    live = [("Deployment", "sre-ai-api"), ("ClusterRole", "sre-ai-something-new"),
            ("ConfigMap", "kube-root-ca.crt")]
    assert module.find_extra(live, described) == [
        {"kind": "ClusterRole", "name": "sre-ai-something-new"}]


def test_known_live_only_entries_carry_a_reason(module):
    """Исключение без причины через полгода — «так исторически сложилось»."""
    assert module.KNOWN_LIVE_ONLY
    for key, reason in module.KNOWN_LIVE_ONLY.items():
        assert len(reason) > 10, key
    assert not any(kind == "Secret" for kind, _ in module.KNOWN_LIVE_ONLY)


def test_kubectl_diff_is_parsed_per_object_and_ignores_generation(module):
    text = "\n".join([
        "diff -u -N /tmp/LIVE-1/apps.v1.Deployment.sre-ai.copilot-beat /tmp/MERGED-2/apps.v1.Deployment.sre-ai.copilot-beat",
        "--- /tmp/LIVE-1/apps.v1.Deployment.sre-ai.copilot-beat\t2026-11-18",
        "+++ /tmp/MERGED-2/apps.v1.Deployment.sre-ai.copilot-beat\t2026-11-18",
        "@@ -5,7 +5,7 @@",
        "-  generation: 188",
        "+  generation: 189",
        "-            name: sre-ai-secrets",
        "+            name: sre-ai-beat-secrets",
        "diff -u -N /tmp/LIVE-1/v1.ConfigMap.sre-ai.redis-config /tmp/MERGED-2/v1.ConfigMap.sre-ai.redis-config",
        "-  generation: 1",
        "+  generation: 2",
        "diff -u -N /tmp/LIVE-1/batch.v1.Job.sre-ai.sre-ai-migrate /tmp/MERGED-2/batch.v1.Job.sre-ai.sre-ai-migrate",
        "+kind: Job",
    ])
    assert module.parse_kubectl_diff(text) == {
        "Deployment/copilot-beat": ["-            name: sre-ai-secrets",
                                    "+            name: sre-ai-beat-secrets"],
    }


def test_declared_namespace_is_respected(module, monkeypatch):
    """jaeger описан в ns monitoring; поиск в ns копилота давал вечное «отсутствует»."""
    module._manifests()
    assert module._DECLARED_NAMESPACE.get(("NetworkPolicy", "github-runner-egress")) == "sre-ai-ci"
    calls = []

    class _Res:
        returncode = 0

    monkeypatch.setattr(module.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _Res())
    module._exists("NetworkPolicy", "github-runner-egress", "sre-ai")
    assert calls[0][-2:] == ["-n", "sre-ai-ci"]


def test_diff_failure_is_reported_not_treated_as_clean(module, monkeypatch, capsys):
    """Не удалось сверить ≠ расхождений нет: об этом надо сказать."""
    monkeypatch.setattr(module, "_manifests", lambda: [])
    monkeypatch.setattr(module, "_live_objects", lambda ns: None)
    monkeypatch.setattr(module, "spec_diff", lambda ns: None)
    monkeypatch.setattr(sys, "argv", ["manifest_drift.py", "--diff"])
    module.main()
    assert "не сверены" in capsys.readouterr().out
