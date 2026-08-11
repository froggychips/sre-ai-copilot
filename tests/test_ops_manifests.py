"""Структурные гейты на ops-зону: манифесты k8s/, чарт helm/, версии тулинга.

Лёгкие проверки без кластера и без рендера-в-облако: yaml.safe_load по файлам
репозитория плюс (если рядом есть бинарь `helm`) один `helm template`.

Что именно держим:

* **Пробы у воркеров.** До 2026-08-10 у copilot-worker и copilot-beat не было
  НИ ОДНОЙ пробы, при полном наборе у api. Воркер, вставший не по памяти
  (CPU-троттлинг, залипший kubectl-subprocess, потерянный broker-коннект),
  оставался Running бесконечно, и при 2 репликах × concurrency=2 тихо выпадала
  половина слотов. Тест не даёт добавить workload без liveness снова.
* **Смысл самой пробы.** `celery inspect ping` без `-d` — это broadcast: за
  зависшую реплику ответит живая соседка, и проба превращается в фикцию. А `-A`
  вместо `-b` заставляет пробу импортировать весь стек приложения (~166 MiB,
  секунды CPU в лимите 600m) каждую минуту. Обе ошибки не ломают рендер и не
  видны глазом в диффе — проверяем текстом.
* **k8s/ ≡ helm/.** Значения расползались уже не раз (resources.worker 768Mi
  против 3Gi в k8s/worker.yaml). Пробы синхронизируем тем же способом.
* **Одна версия python.** ruff.toml/mypy.ini против Dockerfile: расхождение
  означает, что линт и типы проверяют не тот рантайм, в котором код поедет.
* **Секреты чарта.** values-путь должен оставаться ОПЦИОНАЛЬНЫМ: есть
  secrets.existingSecret, secret.yaml под условием, в NOTES.txt — warning.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_K8S_DIR = _REPO_ROOT / "k8s"
_CHART_DIR = _REPO_ROOT / "helm" / "sre-ai-copilot"

_WORKLOAD_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet"})

# Осознанные исключения (file, container) → причина. Пустой список исключений
# был бы честнее, но jaeger — dev-компонент из docker-compose-стека, всё
# состояние в памяти (SPAN_STORAGE_TYPE=memory), деплоится отдельно в ns
# monitoring и не участвует в инцидентном флоу. Добавлять сюда что-то ещё —
# только вместе с объяснением, почему рестарт по liveness хуже тихого зависания.
_LIVENESS_EXEMPT = {
    ("jaeger.yaml", "jaeger"): "dev-only tracing all-in-one, in-memory storage",
}


def _iter_workload_containers():
    """(имя файла, kind, имя workload-а, контейнер) по всем манифестам k8s/."""
    paths = sorted(_K8S_DIR.rglob("*.yaml"))
    assert paths, f"не найдено манифестов в {_K8S_DIR}"
    for path in paths:
        docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("kind") not in _WORKLOAD_KINDS:
                continue
            pod_spec = doc["spec"]["template"]["spec"]
            for container in pod_spec.get("containers", []):
                yield path.name, doc["kind"], doc["metadata"]["name"], container


def _k8s_container(manifest: str, container_name: str) -> dict:
    for name, _kind, _workload, container in _iter_workload_containers():
        if name == manifest and container["name"] == container_name:
            return container
    raise AssertionError(f"{manifest}: контейнер {container_name} не найден")


def test_k8s_workload_containers_have_liveness_probe() -> None:
    """У каждого long-running контейнера в k8s/ есть livenessProbe."""
    missing = [
        f"{name} → {kind}/{workload} → container {container['name']}"
        for name, kind, workload, container in _iter_workload_containers()
        if "livenessProbe" not in container
        and (name, container["name"]) not in _LIVENESS_EXEMPT
    ]
    assert not missing, (
        "workload-контейнеры без livenessProbe (зависший процесс останется "
        "Running навсегда):\n  " + "\n  ".join(missing)
    )


def test_worker_liveness_probe_is_per_node_and_does_not_import_app() -> None:
    """Проба воркера должна спрашивать СВОЙ нод и не тянуть стек приложения."""
    probe = _k8s_container("worker.yaml", "worker")["livenessProbe"]
    command = probe["exec"]["command"]
    script = command[-1]

    assert "inspect ping" in script, "ожидался `celery inspect ping`"
    # Без -d ping отвечает любой живой воркер в кластере → проба бесполезна.
    assert re.search(r"-d\s+\"?celery@", script), (
        "у `inspect ping` нет destination: без -d это broadcast, и за зависшую "
        f"реплику ответит соседка. Команда: {script}"
    )
    # -A app.workers.tasks.celery_app импортирует весь стек на каждую пробу.
    assert " -A " not in f" {script} ", (
        "проба не должна поднимать celery через -A (импорт ~166 MiB и секунды "
        f"CPU каждую минуту) — только -b с broker URL. Команда: {script}"
    )
    assert '"$REDIS_URL"' in script, (
        "broker берём из того же env, что и воркер (envFrom sre-ai-secrets), "
        f"иначе проба смотрит не в тот брокер. Команда: {script}"
    )


def test_worker_liveness_probe_timings_tolerate_slow_start_and_blips() -> None:
    """Таймауты пробы не должны убивать здоровый, но занятый/медленный воркер."""
    probe = _k8s_container("worker.yaml", "worker")["livenessProbe"]
    script = probe["exec"]["command"][-1]

    celery_timeout = re.search(r"-t\s+(\d+)", script)
    assert celery_timeout, f"у пробы нет `-t <sec>`: {script}"
    # kubelet не должен срезать пробу раньше, чем celery успеет вернуть
    # non-zero сам: иначе в событиях вместо причины будет пустой таймаут.
    assert probe["timeoutSeconds"] > int(celery_timeout.group(1)), (
        f"timeoutSeconds={probe['timeoutSeconds']} должен быть больше "
        f"celery -t {celery_timeout.group(1)}"
    )
    # Импорт стека + коннект к брокеру на 600m CPU занимает десятки секунд.
    assert probe["initialDelaySeconds"] >= 90, "мало времени на старт воркера"
    grace = probe["periodSeconds"] * probe["failureThreshold"]
    assert grace >= 300, (
        f"grace={grace}s: liveness на внешней зависимости (redis) с коротким "
        "grace превращает сетевой блип в одновременную рестарт-петлю всех реплик"
    )


def test_beat_liveness_threshold_exceeds_celery_sync_interval() -> None:
    """Порог свежести schedule-файла должен быть заведомо больше sync_every."""
    from celery.beat import Scheduler

    probe = _k8s_container("worker.yaml", "beat")["livenessProbe"]
    script = probe["exec"]["command"][-1]

    # glob, а не точное имя файла: суффикс задаёт dbm-бэкенд (_gdbm — без
    # суффикса, ndbm — .db, dbm.dumb — .dat/.dir/.bak).
    assert "glob" in script and "celerybeat-schedule*" in script, (
        f"ожидался glob по /tmp/celerybeat-schedule*: {script}"
    )
    threshold = re.search(r"age\s*>=\s*(\d+)", script)
    assert threshold, f"в пробе не найден порог staleness: {script}"
    # ×5 к sync_every, а не ×1: celery синкает shelve не по таймеру, а из
    # apply_entry — то есть только когда beat реально что-то отправил. Порог
    # обязан покрывать самый редкий гарантированный тик beat_schedule, иначе
    # проба будет перезапускать здоровый планировщик.
    assert int(threshold.group(1)) >= 5 * Scheduler.sync_every, (
        f"порог {threshold.group(1)}s против celery sync_every="
        f"{Scheduler.sync_every}s — слишком близко к интервалу синка"
    )


@pytest.mark.parametrize(
    "template_name",
    ["deployment-api.yaml", "deployment-worker.yaml", "deployment-beat.yaml"],
)
def test_helm_workload_templates_declare_liveness_probe(template_name: str) -> None:
    """Те же workload-ы в чарте тоже с пробами (chart-install ≠ kubectl apply)."""
    text = (_CHART_DIR / "templates" / template_name).read_text(encoding="utf-8")
    assert "livenessProbe:" in text, f"{template_name}: нет livenessProbe"


def test_helm_probes_match_k8s_manifests() -> None:
    """Пробы в чарте ≡ пробы в k8s/worker.yaml (источник истины — k8s/)."""
    helm = shutil.which("helm")
    if not helm:
        pytest.skip("helm not available")
    rendered = subprocess.run(
        [
            helm, "template", "sre-ai", str(_CHART_DIR),
            "--set", "image.tag=test",
        ],
        capture_output=True,
        text=True,
    )
    assert rendered.returncode == 0, f"helm template упал: {rendered.stderr}"

    chart_probes = {}
    for doc in yaml.safe_load_all(rendered.stdout):
        if not isinstance(doc, dict) or doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            if container["name"] in ("worker", "beat"):
                chart_probes[container["name"]] = container.get("livenessProbe")

    for container_name in ("worker", "beat"):
        expected = _k8s_container("worker.yaml", container_name)["livenessProbe"]
        assert chart_probes.get(container_name) == expected, (
            f"{container_name}: проба в чарте разъехалась с k8s/worker.yaml — "
            "править ОБА файла (как resources.worker)"
        )


def test_python_version_is_consistent_across_toolchain() -> None:
    """Dockerfile (рантайм) ≡ ruff target-version ≡ mypy python_version.

    Расхождение = линт и типы проверяют не тот интерпретатор, в котором код
    поедет: 3.12-only синтаксис проходит гейты и падает SyntaxError уже на
    импорте в проде, то есть CrashLoopBackOff всего деплоймента.
    """
    dockerfile = (_REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    runtime = re.search(r"^FROM\s+python:(\d+)\.(\d+)", dockerfile, re.MULTILINE)
    assert runtime, "не разобрал `FROM python:X.Y...` в Dockerfile"
    major, minor = runtime.group(1), runtime.group(2)

    ruff_toml = (_REPO_ROOT / "ruff.toml").read_text(encoding="utf-8")
    ruff_target = re.search(r'^target-version\s*=\s*"py(\d)(\d+)"', ruff_toml, re.MULTILINE)
    assert ruff_target, "не разобрал target-version в ruff.toml"
    assert (ruff_target.group(1), ruff_target.group(2)) == (major, minor), (
        f"ruff target-version py{ruff_target.group(1)}{ruff_target.group(2)} != "
        f"runtime python {major}.{minor} (Dockerfile)"
    )

    mypy_ini = (_REPO_ROOT / "mypy.ini").read_text(encoding="utf-8")
    mypy_version = re.search(r"^python_version\s*=\s*(\d+)\.(\d+)", mypy_ini, re.MULTILINE)
    assert mypy_version, "не разобрал python_version в mypy.ini"
    assert mypy_version.groups() == (major, minor), (
        f"mypy python_version {mypy_version.group(0)} != runtime python "
        f"{major}.{minor} (Dockerfile)"
    )


def test_helm_secret_values_path_is_optional() -> None:
    """existingSecret — путь по умолчанию для prod, values-путь под условием."""
    values = (_CHART_DIR / "values.yaml").read_text(encoding="utf-8")
    assert re.search(r"^\s+existingSecret:\s*\"\"", values, re.MULTILINE), (
        "в values.yaml нет secrets.existingSecret (ссылка на Secret, созданный "
        "SealedSecrets/ESO) — единственный путь, при котором ключи не оседают "
        "в helm release Secret и в shell history"
    )

    secret_tpl = (_CHART_DIR / "templates" / "secret.yaml").read_text(encoding="utf-8")
    assert "if not .Values.secrets.existingSecret" in secret_tpl, (
        "secret.yaml рендерится всегда: при заданном existingSecret чарт не "
        "должен создавать свой Secret"
    )

    helpers = (_CHART_DIR / "templates" / "_helpers.tpl").read_text(encoding="utf-8")
    assert ".Values.secrets.existingSecret" in helpers, (
        "sre-ai-copilot.secretName не учитывает existingSecret — envFrom "
        "продолжит ссылаться на секрет чарта"
    )


def test_helm_notes_warn_about_values_secrets_path() -> None:
    """NOTES.txt обязан кричать, когда секреты поехали через values."""
    notes = (_CHART_DIR / "templates" / "NOTES.txt").read_text(encoding="utf-8")
    assert "existingSecret" in notes, "NOTES.txt не упоминает безопасный путь"
    assert "WARNING" in notes, "NOTES.txt не предупреждает про values-путь"
