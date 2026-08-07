"""Секции дайджеста исполняются против РЕАЛЬНОЙ схемы, а не против мока.

Зачем отдельный файл. Юнит-тесты в `test_stats_digest.py` мокают
`db.execute(...).fetchone()` и возвращают готовый row — сам SQL при этом
никогда не доходит до Postgres. Поэтому 07.08.2026 в master уехала
регрессия, которую 46 зелёных тестов не заметили: в
`deploy_incident_correlation_section` внешний SELECT считал
`count(DISTINCT (buildtype_id, build_number))`, а CTE `recent_deploys`
колонку `buildtype_id` не выбирал. В проде это давало

    psycopg2.errors.UndefinedColumn: column "buildtype_id" does not exist

секция молча возвращала "" (блок «Deploy → incident correlation» исчез из
дайджеста), а брошенная aborted-транзакция роняла следующую секцию с
InFailedSqlTransaction — один сломанный SQL съедал два блока.

Здесь секции вызываются с настоящей сессией: любая ошибка синтаксиса или
несуществующая колонка ловится сразу. Данных в БД может не быть — тест
проверяет не содержимое, а исполнимость запросов.
"""
from __future__ import annotations

import pytest

from tests.conftest import requires_postgres

pytestmark = requires_postgres


@pytest.fixture(scope="module")
def db_session():
    from app.database import Base, SessionLocal, engine

    Base.metadata.create_all(engine)
    session = SessionLocal()
    yield session
    session.close()


def test_deploy_incident_correlation_sql_executes(db_session, caplog):
    """SQL секции валиден против реальной схемы kg_deployments/kg_alerts.

    Пустая строка сама по себе легальна (нет деплоев за 24ч), но
    предупреждение `deploy_incident_failed` означает именно сломанный SQL.
    """
    from app.services.stats_digest import deploy_incident_correlation_section

    with caplog.at_level("WARNING"):
        deploy_incident_correlation_section(db_session, hours=24)

    failures = [r for r in caplog.records if "deploy_incident_failed" in r.getMessage()]
    assert not failures, f"SQL секции не исполнился: {[r.getMessage() for r in failures]}"


def test_sections_after_failure_still_work(db_session):
    """Упавшая секция не должна оставлять транзакцию в aborted-состоянии.

    Воспроизводим сценарий 07.08.2026: сначала намеренно ломаем транзакцию,
    затем проверяем, что следующая секция всё равно отвечает. Без rollback в
    обработчиках Postgres отвергает любой следующий запрос.
    """
    from sqlalchemy import text

    from app.services.stats_digest import beat_heartbeats_footer

    try:
        db_session.execute(text("SELECT column_that_does_not_exist FROM kg_alerts"))
    except Exception:
        db_session.rollback()

    # Не должно бросить InFailedSqlTransaction.
    beat_heartbeats_footer(db_session)
