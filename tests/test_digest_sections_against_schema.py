"""Каждая секция дайджеста исполняется против РЕАЛЬНОЙ схемы Postgres.

Пункт 2 плана к 1.0. Юнит-тесты секций мокают `db.execute(...).fetchone()`
готовым row, поэтому SQL до Postgres не доходит вообще — 1765 зелёных тестов
спокойно пережили регрессию, где внешний SELECT обращался к колонке
`buildtype_id`, отсутствующей в CTE. В проде это дало
`psycopg2.errors.UndefinedColumn`, секция вернула "" и блок молча исчез.

Здесь каждая секция вызывается с настоящей сессией. Данных в БД может не
быть — проверяется не содержимое, а исполнимость: невалидный SQL, опечатка в
имени колонки или разъехавшаяся схема ловятся сразу.

Контракт секции: поймала ошибку → залогировала `stats_digest.<что>_failed`.
Поэтому наличие такой записи в caplog и есть признак поломки; пустая строка
сама по себе легальна.
"""
from __future__ import annotations

from collections import Counter

import pytest

from tests.conftest import requires_postgres

pytestmark = requires_postgres


@pytest.fixture(scope="module")
def db_session():
    # ВАЖЕН порядок: create_all создаёт только те таблицы, чьи модели уже
    # зарегистрированы в Base.metadata. Без импорта модулей с моделями
    # (kg_* живут в knowledge_graph.schema) таблицы KG не создадутся, и
    # секции упадут на UndefinedTable — что выглядит как «сломанный SQL»,
    # хотя сломан был тестовый стенд.
    import app.knowledge_graph.schema  # noqa: F401
    import app.services.discord.dedup_store  # noqa: F401
    from app.database import Base, SessionLocal, engine

    Base.metadata.create_all(engine)
    session = SessionLocal()
    yield session
    session.close()


def _run_and_collect_failures(call):
    """Вызвать секцию и вернуть список секций, зарегистрировавших сбой.

    Опираемся на трекер из stats_digest (_tx_clean регистрирует имя секции,
    поймавшей ошибку БД), а НЕ на caplog: логи идут через structlog, и
    stdlib-caplog их не видит — первая версия этого теста поэтому молча
    пропускала воспроизведённую регрессию.
    """
    from app.services import stats_digest as sd

    sd._reset_section_failures()
    call()
    try:
        return list(sd._section_failures.get())
    except LookupError:
        return []


# Секции с синхронной сигнатурой: (имя, kwargs без db).
# ns_to_team/unowned/unique_alerts передаём пустыми — секции обязаны
# отрабатывать на пустом входе, это их штатный путь в тихий день.
_SYNC_SECTIONS = [
    ("unowned_namespaces_section", {"unowned": []}),
    ("top_alert_types_section", {"unique_alerts": Counter()}),
    ("fragile_services_section", {"ns_to_team": {}}),
    ("stale_deployments_section", {"ns_to_team": {}, "threshold_days": 30}),
    ("anomaly_summary_section", {}),
    ("anomaly_top_section", {"ns_to_team": {}}),
    ("log_errors_section", {"ns_to_team": {}}),
    ("kg_quality_section", {}),
    ("action_items_section", {}),
    ("mttr_section", {"days": 7}),
    ("deploy_incident_correlation_section", {"hours": 24}),
    ("pipeline_health_section", {}),
    ("beat_heartbeats_footer", {}),
]


@pytest.mark.parametrize("section_name,kwargs", _SYNC_SECTIONS, ids=[s[0] for s in _SYNC_SECTIONS])
def test_sync_section_sql_is_valid(section_name, kwargs, db_session):
    """SQL секции валиден против живой схемы."""
    from app.services import stats_digest as sd

    section = getattr(sd, section_name)
    # Порядок аргументов у секций разный (db первым, db последним, db=None) —
    # передаём именованным, так тест не зависит от позиции.
    failures = _run_and_collect_failures(lambda: section(db=db_session, **kwargs))

    assert not failures, f"{section_name}: секция сообщила о сбое → {failures}"


@pytest.mark.asyncio
async def test_topology_growth_section_sql_is_valid(db_session):
    """Async-секция topology_growth — тот же контракт."""
    from app.services import stats_digest as sd

    sd._reset_section_failures()
    await sd.topology_growth_section(db=db_session)
    try:
        failures = list(sd._section_failures.get())
    except LookupError:
        failures = []

    assert not failures, f"topology_growth_section: секция сообщила о сбое → {failures}"


@pytest.mark.asyncio
async def test_cluster_health_section_sql_is_valid(db_session):
    """cluster_health получает VM-клиент; интересует только БД-часть.

    VMClient c недостижимым URL отдаст свои ошибки (это не наш контракт), а вот
    kube-state-часть секции обязана исполниться против схемы.
    """
    from app.context.vm_client import VMClient
    from app.services import stats_digest as sd

    vm = VMClient("http://127.0.0.1:1", timeout=0.1)
    sd._reset_section_failures()
    await sd.cluster_health_section(vm=vm, fired_series=[], db=db_session)
    try:
        failures = list(sd._section_failures.get())
    except LookupError:
        failures = []

    assert not failures, f"cluster_health_section: секция сообщила о сбое → {failures}"


def test_all_db_sections_are_covered():
    """Список секций в тесте не отстаёт от кода.

    Иначе новая секция появится, тестом покрыта не будет, и её SQL опять
    поедет в прод непроверенным — ровно сценарий 07.08.2026.
    """
    import ast
    import pathlib

    src = pathlib.Path("app/services/stats_digest.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # публичные функции, принимающие db, кроме точек входа сборки/отправки
    entrypoints = {"build_digest", "send_daily_digest"}
    in_code = {
        fn.name
        for fn in tree.body
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not fn.name.startswith("_")
        and "db" in [a.arg for a in fn.args.args]
        and fn.name not in entrypoints
    }
    covered = {name for name, _ in _SYNC_SECTIONS} | {
        "topology_growth_section",
        "cluster_health_section",
    }

    missing = in_code - covered
    assert not missing, (
        f"секции без интеграционного теста: {sorted(missing)} — "
        "добавь их в _SYNC_SECTIONS или в async-тесты"
    )
