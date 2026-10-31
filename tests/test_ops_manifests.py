"""Структурные гейты на ops-зону: манифесты k8s/, версии тулинга.

Лёгкие проверки без кластера и без рендера-в-облако: yaml.safe_load по файлам
репозитория.

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
* **Одна версия python.** ruff.toml/mypy.ini против Dockerfile: расхождение
  означает, что линт и типы проверяют не тот рантайм, в котором код поедет.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_K8S_DIR = _REPO_ROOT / "k8s"

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


def test_ci_python_matches_runtime() -> None:
    """CI гоняет тесты на той же версии, что стоит в Dockerfile.

    Тест выше держит вместе Dockerfile, ruff и mypy — но не CI, и потому
    расхождение жило молча: линтеры были настроены на 3.11, а прогон шёл на
    3.12, то есть тесты проверяли не тот интерпретатор, в котором код едет.
    Закрыто 17.09.2026; без этой проверки вернётся при первой же правке
    workflow.

    Версию задаёт `uv venv --python X.Y`. Homebrew-путь в workflow —
    отдельный запрет: сборка python@3.11 из homebrew под macOS 26.1
    нерабочая (`platform.mac_ver()` пуст), и возврат к ней снова уведёт CI
    на другую версию.
    """
    dockerfile = (_REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    runtime = re.search(r"^FROM\s+python:(\d+)\.(\d+)", dockerfile, re.MULTILINE)
    assert runtime, "не разобрал `FROM python:X.Y...` в Dockerfile"
    expected = f"{runtime.group(1)}.{runtime.group(2)}"

    workflows = sorted((_REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows, "не найдено ни одного workflow"

    checked = 0
    for wf in workflows:
        text = wf.read_text(encoding="utf-8")
        for found in re.findall(r"uv venv --python (\d+\.\d+)", text):
            checked += 1
            assert found == expected, (
                f"{wf.name}: CI ставит python {found}, а рантайм "
                f"{expected} (Dockerfile)"
            )
        for path in re.findall(r"/opt/homebrew/bin/python3\.\d+", text):
            raise AssertionError(
                f"{wf.name}: {path} — homebrew-питон в CI. Версию ставит uv; "
                "сборка python@3.11 из homebrew под macOS 26 нерабочая."
            )

    assert checked, "ни один workflow не задаёт версию python через uv"



def test_worker_exposes_metrics_port() -> None:
    """У воркера объявлен порт метрик, иначе скрейпить нечего.

    VMPodScrape перечисляет `copilot-worker` в селекторе с 05.09.2026, но
    сервер метрик поднимался только в API-процессе: 17.09.2026 в
    VictoriaMetrics не было ни одной серии `llm_*`, хотя весь LLM-слой
    считается именно в воркере.
    """
    import yaml

    docs = [
        d for d in yaml.safe_load_all(
            (_REPO_ROOT / "k8s" / "worker.yaml").read_text(encoding="utf-8")
        ) if d
    ]
    workers = [
        d for d in docs
        if d.get("kind") == "Deployment"
        and d["metadata"]["name"] == "copilot-worker"
    ]
    assert workers, "не найден Deployment copilot-worker"
    container = workers[0]["spec"]["template"]["spec"]["containers"][0]

    ports = {p.get("name"): p.get("containerPort") for p in container.get("ports", [])}
    assert ports.get("metrics") == 8001, (
        f"порт metrics:8001 не объявлен у воркера (есть: {ports})"
    )

    env = {e["name"]: e.get("value") for e in container.get("env", [])}
    assert env.get("PROMETHEUS_MULTIPROC_DIR"), (
        "без PROMETHEUS_MULTIPROC_DIR сервер отдаёт реестр родителя, "
        "где задачи не исполняются — то есть ровные нули"
    )
    mounts = {m["mountPath"] for m in container.get("volumeMounts", [])}
    assert any(env["PROMETHEUS_MULTIPROC_DIR"].startswith(m) for m in mounts), (
        f"каталог {env['PROMETHEUS_MULTIPROC_DIR']} вне смонтированных томов "
        f"({mounts}), а readOnlyRootFilesystem запрещает запись"
    )
