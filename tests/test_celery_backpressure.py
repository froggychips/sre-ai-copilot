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
