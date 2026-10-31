"""Тесты на backpressure-конфигурацию Celery worker'а.

Проверяем что в non-eager режиме применены настройки защиты от:
- flood (prefetch_multiplier=1)
- memory leak (max_tasks_per_child=50)
- зависших задач (time_limit + soft_time_limit)
- LLM-бюджет flood (rate_limit на process_incident)

Плюс изоляция async-клиентов per event loop: Celery-таски гоняются через
`asyncio.run(...)` (новый loop на задачу) при живущем до 50 задач процессе —
модульные клиенты с одним connection pool давали "Event loop is closed"
со второй задачи и молча выключали circuit breaker.
"""
import asyncio
from unittest.mock import MagicMock, patch

import pytest


def test_celery_backpressure_config_applied_in_prod_mode():
    """В non-eager mode → backpressure-настройки применены к celery_app."""
    # Свежий импорт с CELERY_TASK_ALWAYS_EAGER=False (default prod)
    import importlib

    import app.workers.tasks as tasks_mod
    with patch.object(tasks_mod.settings, "CELERY_TASK_ALWAYS_EAGER", False):
        importlib.reload(tasks_mod)
        conf = tasks_mod.celery_app.conf
        assert conf.worker_prefetch_multiplier == 1
        assert conf.worker_max_tasks_per_child == 50
        assert conf.task_time_limit == 1800
        assert conf.task_soft_time_limit == 1500
        assert conf.broker_connection_retry_on_startup is True


def test_celery_acks_late_enabled_in_prod_mode():
    """acks_late + reject_on_worker_lost — задача переживает смерть воркера.

    При дефолтном acks_early брокер подтверждает сообщение в момент ВЫДАЧИ:
    воркер, убитый в середине обработки (OOMKill / eviction / rollout),
    уносит задачу с собой — инцидент не обработан, ретрая нет, следа нет.
    Тест фиксирует настройку, чтобы её не потеряли молча при следующей
    правке конфига.
    """
    import importlib

    import app.workers.tasks as tasks_mod
    with patch.object(tasks_mod.settings, "CELERY_TASK_ALWAYS_EAGER", False):
        importlib.reload(tasks_mod)
        conf = tasks_mod.celery_app.conf
        assert conf.task_acks_late is True
        # Без reject_on_worker_lost потерянная задача уходит в FAILURE,
        # а не возвращается в очередь — то есть теряется всё равно.
        assert conf.task_reject_on_worker_lost is True
        # Обычный провал и срабатывание time limit'а задачу ПОДТВЕРЖДАЮТ:
        # иначе стабильно падающий таск крутится в очереди вечно
        # (poison message). Ретраями управляет autoretry_for, не брокер.
        assert conf.task_acks_on_failure_or_timeout is True


def test_redis_visibility_timeout_exceeds_hard_time_limit():
    """visibility_timeout > hard time limit — иначе acks_late даёт ДУБЛЬ.

    У redis-транспорта нет настоящего ack: неподтверждённое сообщение
    возвращается в очередь по visibility_timeout. Если окно короче времени
    выполнения, redis отдаёт задачу второму воркеру, пока первый ещё
    работает — два параллельных прогона одного инцидента.
    """
    import importlib

    import app.workers.tasks as tasks_mod
    with patch.object(tasks_mod.settings, "CELERY_TASK_ALWAYS_EAGER", False):
        importlib.reload(tasks_mod)
        conf = tasks_mod.celery_app.conf
        visibility = conf.broker_transport_options["visibility_timeout"]
        assert visibility > conf.task_time_limit


def test_process_incident_has_rate_limit():
    """process_incident task должен иметь rate_limit для защиты LLM-бюджета."""
    from app.workers.tasks import process_incident_task

    # Celery хранит rate_limit как атрибут task
    assert process_incident_task.rate_limit is not None
    # Default = "30/m"
    assert "/m" in str(process_incident_task.rate_limit)


def test_tc_deploys_to_kg_beat_scheduled():
    """Новый KG event-store beat: tc_deploys_to_kg каждые 15 мин."""
    from app.workers.tasks import celery_app

    schedule = celery_app.conf.beat_schedule
    assert "tc-deploys-to-kg" in schedule
    assert schedule["tc-deploys-to-kg"]["task"] == "tc_deploys_to_kg"


def test_tc_deploys_to_kg_task_no_op_without_tc():
    """Когда TC ничего не вернул — task завершается без edges-added."""
    from unittest.mock import AsyncMock, patch

    from app.workers.tasks import _tc_deploys_to_kg_logic

    with patch(
        "app.services.teamcity_service.recent_deploys",
        new=AsyncMock(return_value=[]),
    ):
        import asyncio
        result = asyncio.run(_tc_deploys_to_kg_logic())
    assert result["builds_fetched"] == 0
    assert result["kg_deployments_added"] == 0


# ── Per-loop изоляция async-клиентов ────────────────────────────────────────


def test_loop_local_redis_not_reused_across_asyncio_runs():
    """Последовательные asyncio.run НЕ переиспользуют клиент мёртвого loop-а.

    Внутри одного loop-а клиент кэшируется (один pool на задачу), между
    loop-ами — создаётся заново. Именно переиспользование pool-а от
    закрытого loop-а давало "Event loop is closed" и no-op circuit breaker.
    """
    from app.services.resilience import LoopLocalRedis

    created: list = []

    def fake_from_url(url):
        client = MagicMock(name=f"redis-client-{len(created)}")
        created.append(client)
        return client

    with patch("app.services.resilience.from_url", side_effect=fake_from_url):
        proxy = LoopLocalRedis("redis://test:6379/0")

        async def resolve_twice():
            return proxy._resolve(), proxy._resolve()

        a1, a2 = asyncio.run(resolve_twice())
        b1, b2 = asyncio.run(resolve_twice())

    assert a1 is a2  # в пределах одного loop-а — один клиент
    assert b1 is b2
    assert a1 is not b1  # новый loop → новый клиент, не мёртвый pool
    assert len(created) == 2


def test_loop_local_redis_sync_context_uses_fallback():
    """Вне event loop-а прокси отдаёт общий fallback-клиент (sync-контекст)."""
    from app.services.resilience import LoopLocalRedis

    with patch(
        "app.services.resilience.from_url",
        side_effect=lambda url: MagicMock(),
    ):
        proxy = LoopLocalRedis("redis://test:6379/0")
        c1 = proxy._resolve()
        c2 = proxy._resolve()
    assert c1 is c2


def test_llm_service_anthropic_client_is_per_event_loop():
    """LLMService: AsyncAnthropic создаётся per-loop, а не один на процесс.

    httpx-pool и asyncio-локи внутри AsyncAnthropic привязаны к loop-у
    создания — переживший `asyncio.run` клиент ломает следующую задачу.
    """
    from app.services.llm_service import LLMService

    with patch("app.services.llm_service.settings") as ms:
        ms.LLM_BACKEND = "anthropic"
        ms.MODEL_NAME = "claude-sonnet-4-6"
        ms.ANTHROPIC_API_KEY = "test-key"
        ms.LLM_TIMEOUT_SECONDS = 30.0

        svc = LLMService()

        async def client_twice():
            return svc._anthropic_client(), svc._anthropic_client()

        a1, a2 = asyncio.run(client_twice())
        b1, _ = asyncio.run(client_twice())

    assert a1 is a2  # внутри loop-а — один клиент
    assert a1 is not b1  # новый loop → новый клиент


def test_llm_service_explicit_client_override_wins():
    """svc.client = <mock> (тестовый/кастомный клиент) перекрывает per-loop кэш."""
    from app.services.llm_service import LLMService

    with patch("app.services.llm_service.settings") as ms:
        ms.LLM_BACKEND = "anthropic"
        ms.MODEL_NAME = "claude-sonnet-4-6"
        ms.ANTHROPIC_API_KEY = "test-key"
        ms.LLM_TIMEOUT_SECONDS = 30.0

        svc = LLMService()
        override = MagicMock(name="explicit-client")
        svc.client = override

        async def get_client():
            return svc._anthropic_client()

        assert asyncio.run(get_client()) is override


# ── Очередь `sync`: тяжёлые синки отдельно от пайплайна ────────────────────
#
# Аудит 31.10.2026: четыре слота copilot-worker делили 35 beat-задач и
# process_incident; часовой kg_topology_sync (4–10 мин) и 15-минутный
# tc_deploys_to_kg занимали слоты, минутная kg_external_probe (expires 50 с)
# и инцидент ждали за ними. Тяжёлые уезжают в очередь `sync` к отдельному
# deployment copilot-worker-sync; маршрут — task_routes, а не queue= в
# apply_async, чтобы beat и api об очереди не знали.

#: Минимум, согласованный с владельцем: эти задачи обязаны быть в `sync`.
#: Расширять список можно (SYNC_QUEUE_TASKS), сужать — только вместе с ним.
_SYNC_CORE = frozenset({
    "kg_topology_sync",
    "tc_deploys_to_kg",
    "kg_storage_sync",
    "kg_metrics_sync",
    "kg_endpoints_sync",
    "kg_signal_aggregates_compute",
    "kg_health_retention",
})

#: Задачам, которым важна задержка старта, в `sync` не место: там они
#: встанут за топологией. process_incident — пайплайн инцидента.
_LATENCY_SENSITIVE = frozenset({
    "process_incident",
    "kg_external_probe",
    "kg_deploy_watch",
    "kg_incidents_lifecycle",
    "kg_cluster_health_sync",
})


def test_heavy_syncs_are_routed_to_sync_queue():
    from app.config import settings
    from app.workers.tasks import SYNC_QUEUE_TASKS, celery_app

    routes = celery_app.conf.task_routes
    assert _SYNC_CORE <= SYNC_QUEUE_TASKS, sorted(_SYNC_CORE - SYNC_QUEUE_TASKS)
    for name in SYNC_QUEUE_TASKS:
        assert routes.get(name, {}).get("queue") == settings.SYNC_QUEUE_NAME, (
            f"{name} объявлена тяжёлой, но task_routes не ведёт её в "
            f"{settings.SYNC_QUEUE_NAME!r}"
        )
    # В task_routes нет ничего, кроме SYNC_QUEUE_TASKS: второй источник
    # правды о маршрутах — ровно то, от чего уходим.
    assert set(routes) == set(SYNC_QUEUE_TASKS)


def test_sync_queue_tasks_are_registered_beat_tasks():
    """Опечатка в имени задачи в SYNC_QUEUE_TASKS не должна молча оставить
    её в `celery` — маршрут по несуществующему имени celery не проверяет."""
    from app.workers.tasks import SYNC_QUEUE_TASKS, celery_app

    beat = {cfg["task"] for cfg in celery_app.conf.beat_schedule.values()}
    assert SYNC_QUEUE_TASKS <= set(celery_app.tasks), (
        sorted(SYNC_QUEUE_TASKS - set(celery_app.tasks))
    )
    assert SYNC_QUEUE_TASKS <= beat, sorted(SYNC_QUEUE_TASKS - beat)


def test_latency_sensitive_tasks_stay_in_default_queue():
    from app.workers.tasks import SYNC_QUEUE_TASKS, celery_app

    assert celery_app.conf.task_default_queue == "celery"
    leaked = sorted(_LATENCY_SENSITIVE & SYNC_QUEUE_TASKS)
    assert not leaked, (
        f"{leaked}: задачам с требованием к задержке не место в sync — "
        "там они встанут за kg_topology_sync"
    )


def test_sync_queue_name_is_a_setting_like_executor():
    """Имя очереди — из Settings (как EXECUTOR_QUEUE_NAME), не литерал."""
    from app.config import Settings

    assert Settings.model_fields["SYNC_QUEUE_NAME"].default == "sync"
    assert Settings.model_fields["EXECUTOR_QUEUE_NAME"].default == "executor"


# ── Per-task лимиты у тяжёлых ────────────────────────────────────────────────


def _tasks_with_own_limits():
    from app.workers.tasks import celery_app
    out = []
    for cfg in celery_app.conf.beat_schedule.values():
        task = celery_app.tasks[cfg["task"]]
        if task.time_limit is not None or task.soft_time_limit is not None:
            out.append((cfg["task"], task, cfg))
    return out


def test_some_heavy_tasks_declare_their_own_limits():
    """Иначе проверки ниже молча проходили бы по пустому списку."""
    names = {name for name, _t, _c in _tasks_with_own_limits()}
    assert {"tc_deploys_to_kg", "kg_metrics_sync", "kg_endpoints_sync"} <= names


@pytest.mark.parametrize("name,task,cfg", _tasks_with_own_limits(),
                         ids=[n for n, _t, _c in _tasks_with_own_limits()])
def test_per_task_limits_are_consistent(name, task, cfg):
    """soft < hard ≤ глобальный; свой лимит только у задач очереди sync.

    Глобальные 1500/1800 — потолок (visibility_timeout = 2×hard считается
    от них); per-task лимит выше глобального дал бы повторную выдачу
    задачи вторым воркером посреди прогона.
    """
    from app.config import settings
    from app.workers.tasks import SYNC_QUEUE_TASKS

    assert task.soft_time_limit is not None and task.time_limit is not None, (
        f"{name}: задавать нужно оба лимита — soft даёт задаче закрыть сессию"
    )
    assert task.soft_time_limit < task.time_limit
    assert task.time_limit <= settings.CELERY_TASK_TIME_LIMIT_SECONDS
    assert task.soft_time_limit <= settings.CELERY_TASK_SOFT_TIME_LIMIT_SECONDS
    assert name in SYNC_QUEUE_TASKS, (
        f"{name}: свой лимит есть у тяжёлых задач очереди sync; лёгким "
        "хватает глобального"
    )
    # Лок обязан пережить самый долгий разрешённый прогон: иначе второй
    # экземпляр стартует поверх живого первого до того, как тот снимут.
    assert task.run.lock_ttl_seconds >= task.time_limit, (
        f"{name}: lock ttl {task.run.lock_ttl_seconds} < hard limit "
        f"{task.time_limit}"
    )


def test_per_task_limits_survive_eager_mode():
    """В eager-режиме (тесты, локальный e2e) лимиты — атрибуты задачи, не
    настройки воркера: задача исполняется на месте, и падать по ним нечему."""
    import importlib

    import app.workers.tasks as tasks_mod
    with patch.object(tasks_mod.settings, "CELERY_TASK_ALWAYS_EAGER", True):
        importlib.reload(tasks_mod)
        assert tasks_mod.celery_app.conf.task_always_eager is True
        assert tasks_mod.tc_deploys_to_kg_task.time_limit == 720
        # Маршруты тоже на месте — безвредны, задача исполняется inline.
        assert "tc_deploys_to_kg" in tasks_mod.celery_app.conf.task_routes
    importlib.reload(tasks_mod)
