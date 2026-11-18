"""LLM-пайплайн не грузится в процесс, пока он выключен.

В проде `LLM_PIPELINE_ENABLED=False`, но до 18.11.2026 `app.workers.tasks`
импортировал `app.workers.pipeline` на уровне модуля, а `app.celery_worker` —
`AnalyzerAgent`. Вслед за ними в КАЖДЫЙ процесс (worker, worker-sync,
executor, beat, api) приезжали агенты, llm_service, SDK anthropic и ещё
~6,7 тыс. строк, достижимых только через эти два модуля. Для prefork-воркера
это база памяти каждого форка.

Что здесь закреплено:

  * три точки входа прода (worker/executor, beat, api) после старта НЕ держат
    в `sys.modules` ни одного модуля LLM-контура;
  * задачи при этом зарегистрированы (имена не менялись), а первый же доступ
    к пайплайну его подгружает — то есть проверка способна упасть;
  * мост remediation и предикат ретраев живут без LLM-слоя и без SDK.

Поведение `/copilot`, `/replay/{id}` и `generate_reply` при выключенном флаге
проверяет tests/test_copilot_disabled_gate.py.

ПОЧЕМУ SUBPROCESS. pytest к моменту запуска этого файла уже импортировал
пол-приложения (включая сам пайплайн — его тянут соседние тесты), поэтому
`sys.modules` текущего процесса ничего не доказывает. Каждая проверка
запускает ЧИСТЫЙ интерпретатор и повторяет в нём ровно то, что делает
прод-процесс: `celery -A app.workers.tasks.celery_app worker|beat` →
`find_app` + `import_default_modules` + `finalize`; `uvicorn app.main:app` →
`uvicorn.importer.import_from_string`.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
ROOT = Path(__file__).resolve().parents[1]

# Собственно LLM-контур: пайплайн, агенты, клиенты модели и их обвязка.
# Появление любого из них в процессе с выключенным флагом — регрессия.
LLM_LAYER = (
    "app.workers.pipeline",
    "app.workers.report_delivery",
    "app.agents",
    "app.llm",
    "app.services.llm_service",
    "app.services.claude_cli_service",
    "app.services.prompt_guard",
    "app.services.session_manager",
    "app.core.tracing",
    "app.core.intelligence",
    "app.context.context_builder",
    "app.context.incident_snapshot",
    "app.evaluation.llm_replay",
)

# Модули без слова «LLM» в названии, но достижимые сегодня ТОЛЬКО через
# пайплайн (граф импортов на 18.11.2026). Если живой код начнёт
# использовать какой-то из них — убрать его отсюда осознанно, тем же PR.
REACHABLE_ONLY_VIA_PIPELINE = (
    "app.context.logs",
    "app.context.metrics",
    "app.diagnostics.incident_ctx",
    "app.rca",
    "app.services.clickhouse_service",
    "app.services.gitlab_service",
)

FORBIDDEN_APP = LLM_LAYER + REACHABLE_ONLY_VIA_PIPELINE

# Сторонние пакеты, которые нужны только LLM-контуру.
FORBIDDEN_THIRD_PARTY = ("anthropic",)

_PRELUDE = f"""
import json, sys
FORBIDDEN = {FORBIDDEN_APP + FORBIDDEN_THIRD_PARTY!r}

def leaked():
    return sorted(
        m for m in sys.modules
        if any(m == p or m.startswith(p + ".") for p in FORBIDDEN)
    )

def celery_entry():
    # То же, что делает `celery -A app.workers.tasks.celery_app <cmd>`.
    from celery.app.utils import find_app
    app = find_app("app.workers.tasks.celery_app")
    app.loader.import_default_modules()
    app.finalize()
    return app

def worker_boot(app):
    # Главный процесс worker-а шлёт worker_init перед созданием пула
    # (celery.apps.worker.Worker.on_before_init); beat этого не делает.
    from celery.signals import worker_init
    worker_init.send(sender=None)
"""

_WORKER = _PRELUDE + """
app = celery_entry()
worker_boot(app)
out = {"leaked": leaked(), "tasks": sorted(t for t in app.tasks if not t.startswith("celery.")),
       "kubernetes": "kubernetes.client" in sys.modules}
# Контроль: проверка обязана уметь падать. Первый доступ к пайплайну через
# модуль задач должен его загрузить — иначе список FORBIDDEN ничего не ловит.
import app.workers.tasks as tasks
tasks.IncidentPipeline
out["after_access"] = "app.workers.pipeline" in sys.modules and "anthropic" in sys.modules
print("RESULT=" + json.dumps(out))
"""

_BEAT = _PRELUDE + """
app = celery_entry()
schedule = app.conf.beat_schedule  # beat читает расписание при старте
out = {"leaked": leaked(), "schedule_entries": len(schedule),
       "kubernetes": "kubernetes" in sys.modules,
       "tasks": sorted(t for t in app.tasks if not t.startswith("celery."))}
print("RESULT=" + json.dumps(out))
"""

_API = _PRELUDE + """
from uvicorn.importer import import_from_string
asgi = import_from_string("app.main:app")  # то же, что `uvicorn app.main:app`
routes = sorted(getattr(r, "path", "") for r in asgi.routes)
out = {"leaked": leaked(), "has_copilot": "/copilot" in routes}
import app.celery_worker as cw
cw.AnalyzerAgent
out["after_access"] = "app.agents.analyzer" in sys.modules
print("RESULT=" + json.dumps(out))
"""

_BRIDGE = _PRELUDE + """
# Мост remediation делит с пайплайном build_match_snapshot и
# bind_server_params — они должны жить без LLM-слоя.
import app.remediation.bridge
from app.remediation.binding import bind_server_params, build_match_snapshot
out = {"leaked": leaked(), "callable": callable(bind_server_params) and callable(build_match_snapshot)}
print("RESULT=" + json.dumps(out))
"""


def _run_clean(script: str) -> dict:
    """Запустить скрипт в чистом интерпретаторе с продовым положением флага."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "DATABASE_URL"}
    }
    env.update(
        PYTHONPATH=str(ROOT),
        LLM_PIPELINE_ENABLED="False",
        LLM_BACKEND="claude_cli",
        DISCORD_WEBHOOK_URL="https://example.com/test-webhook",
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, f"subprocess failed:\n{proc.stdout}\n{proc.stderr}"
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT=")]
    assert lines, f"no RESULT line:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(lines[-1][len("RESULT="):])


# ── список запрещённого сам не должен протухнуть ─────────────────────────


@pytest.mark.parametrize("name", FORBIDDEN_APP + FORBIDDEN_THIRD_PARTY)
def test_forbidden_names_exist(name: str) -> None:
    """Переименованный модуль молча выпал бы из проверки — ловим это здесь."""
    assert importlib.util.find_spec(name) is not None, (
        f"{name} больше не существует: обнови список в этом файле"
    )


# ── три точки входа прода ────────────────────────────────────────────────


def test_celery_worker_entrypoint_does_not_load_llm_layer() -> None:
    """worker, worker-sync и executor: `celery -A app.workers.tasks.celery_app worker`."""
    out = _run_clean(_WORKER)
    assert out["leaked"] == [], f"LLM-слой загружен в worker: {out['leaked']}"
    # Имена задач не менялись, и LLM-задачи по-прежнему зарегистрированы.
    for task in ("process_incident", "generate_reply", "remediation_bridge",
                 "executor_apply", "executor_dry_run"):
        assert task in out["tasks"], f"задача {task} не зарегистрирована"
    assert out["after_access"] is True, (
        "доступ к tasks.IncidentPipeline не подгрузил пайплайн — контроль сломан"
    )
    # Клиент kubernetes нужен живым задачам и должен попасть в родителя до
    # форка (общая память форков), а не грузиться каждым форком заново.
    assert out["kubernetes"] is True


def test_celery_beat_entrypoint_does_not_load_llm_layer() -> None:
    """beat: `celery -A app.workers.tasks.celery_app beat` (лимит памяти 256Mi)."""
    out = _run_clean(_BEAT)
    assert out["leaked"] == [], f"LLM-слой загружен в beat: {out['leaked']}"
    assert out["schedule_entries"] > 0
    # beat задач не исполняет: клиент kubernetes ему не нужен вовсе.
    assert out["kubernetes"] is False, "beat держит клиент kubernetes в памяти"
    assert "process_incident" in out["tasks"]


def test_api_entrypoint_does_not_load_llm_layer() -> None:
    """api: `uvicorn app.main:app`."""
    out = _run_clean(_API)
    assert out["leaked"] == [], f"LLM-слой загружен в api: {out['leaked']}"
    assert out["has_copilot"] is True
    assert out["after_access"] is True


def test_remediation_bridge_does_not_need_llm_layer() -> None:
    out = _run_clean(_BRIDGE)
    assert out["leaked"] == [], f"мост remediation тянет LLM-слой: {out['leaked']}"
    assert out["callable"] is True


# ── подмена ленивых имён в тестах продолжает работать ────────────────────


def test_lazy_names_are_still_patchable(monkeypatch) -> None:
    """`patch("app.workers.tasks.IncidentPipeline")` — на нём стоят чужие тесты."""
    import app.celery_worker as cw
    import app.workers.pipeline as pipeline
    import app.workers.tasks as tasks

    assert tasks.IncidentPipeline is pipeline.IncidentPipeline
    assert tasks.transition_to is pipeline.transition_to
    assert tasks._pipeline_attr("IncidentPipeline") is pipeline.IncidentPipeline

    sentinel = object()
    monkeypatch.setattr(tasks, "IncidentPipeline", sentinel)
    assert tasks._pipeline_attr("IncidentPipeline") is sentinel

    monkeypatch.setattr(cw, "AnalyzerAgent", sentinel)
    assert cw._lazy("AnalyzerAgent") is sentinel

    with pytest.raises(AttributeError):
        tasks.no_such_attribute  # noqa: B018
    with pytest.raises(AttributeError):
        cw.no_such_attribute  # noqa: B018


# ── resilience без SDK ───────────────────────────────────────────────────


def test_retry_predicate_works_without_anthropic_loaded(monkeypatch) -> None:
    from app.services import resilience

    monkeypatch.setattr(resilience, "_anthropic_sdk", lambda: None)
    assert resilience.is_retryable_llm_error(asyncio.TimeoutError()) is True
    assert resilience.is_retryable_llm_error(ValueError("bad request")) is False
    wrapped = ValueError("LLM timeout")
    wrapped.__cause__ = asyncio.TimeoutError()
    assert resilience.is_retryable_llm_error(wrapped) is True


def test_retry_predicate_sees_sdk_once_loaded() -> None:
    """Сами классы ошибок anthropic покрыты в test_llm_retry_predicate.py."""
    import anthropic

    from app.services import resilience

    assert resilience._anthropic_sdk() is anthropic
