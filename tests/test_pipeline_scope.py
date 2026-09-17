"""Область действия пайплайна: что доходит до LLM, а что нет.

Третье условие включения `LLM_PIPELINE_ENABLED` — «severity-фильтр сужен
до critical + prod-*». Проверяется и сам фильтр, и то, что он стоит на
пути, мимо которого не пройти.
"""
import pytest

from app.config import settings
from app.workers.pipeline_scope import check_scope


@pytest.fixture
def default_scope(monkeypatch):
    monkeypatch.setattr(settings, "PIPELINE_SEVERITY_ALLOWLIST", ["critical"], raising=False)
    monkeypatch.setattr(settings, "PIPELINE_NAMESPACE_PREFIXES", ["prod-"], raising=False)


def test_critical_in_prod_passes(default_scope):
    verdict = check_scope({"severity": "critical", "namespace": "prod-k5"})
    assert verdict.in_scope


def test_warning_in_prod_is_filtered(default_scope):
    verdict = check_scope({"severity": "warning", "namespace": "prod-k5"})
    assert not verdict.in_scope
    assert verdict.reason == "severity_out_of_scope"


def test_critical_outside_prod_is_filtered(default_scope):
    verdict = check_scope({"severity": "critical", "namespace": "squad-29"})
    assert not verdict.in_scope
    assert verdict.reason == "namespace_out_of_scope"


def test_severity_read_from_labels_when_field_absent(default_scope):
    """В пайплайн приходит и dict, собранный не из Incident-модели."""
    verdict = check_scope({"labels": {"severity": "critical", "namespace": "prod-k5"}})
    assert verdict.in_scope


def test_case_and_spaces_do_not_smuggle_alerts(default_scope):
    """`Critical` и `critical` — одно и то же; иначе фильтр обходится регистром."""
    assert check_scope({"severity": " CRITICAL ", "namespace": "prod-k5"}).in_scope


def test_missing_severity_does_not_pass(default_scope):
    """Отсутствующая метка — не повод тратить бюджет.

    Обратное решение (пропускать неизвестное) означало бы, что любой алерт
    без лейбла обходит фильтр целиком.
    """
    verdict = check_scope({"namespace": "prod-k5"})
    assert not verdict.in_scope
    assert verdict.severity == "unknown"


def test_missing_namespace_does_not_pass(default_scope):
    assert not check_scope({"severity": "critical"}).in_scope


def test_empty_payload_does_not_pass(default_scope):
    assert not check_scope(None).in_scope
    assert not check_scope({}).in_scope


def test_empty_allowlist_disables_only_its_own_dimension(monkeypatch):
    """Пустой список выключает фильтр по СВОЕМУ измерению, не по обоим."""
    monkeypatch.setattr(settings, "PIPELINE_SEVERITY_ALLOWLIST", [], raising=False)
    monkeypatch.setattr(settings, "PIPELINE_NAMESPACE_PREFIXES", ["prod-"], raising=False)

    assert check_scope({"severity": "info", "namespace": "prod-k5"}).in_scope
    assert not check_scope({"severity": "critical", "namespace": "dev-17"}).in_scope


def test_prefix_match_is_not_substring_match(default_scope):
    """`prod-` матчит началом строки, а не вхождением.

    Иначе `squad-prod-test` прошёл бы как продовый.
    """
    assert not check_scope({"severity": "critical", "namespace": "squad-prod-test"}).in_scope


@pytest.mark.asyncio
async def test_scope_gate_blocks_pipeline_entry(monkeypatch):
    """Фильтр стоит на входе в задачу — мимо него не пройти ни одним путём."""
    from app.workers import tasks

    monkeypatch.setattr(settings, "LLM_PIPELINE_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "PIPELINE_SEVERITY_ALLOWLIST", ["critical"], raising=False)
    monkeypatch.setattr(settings, "PIPELINE_NAMESPACE_PREFIXES", ["prod-"], raising=False)

    result = await tasks.async_process_incident(
        {"incident_id": "x", "severity": "warning", "namespace": "dev-17"}
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "severity_out_of_scope"


@pytest.mark.asyncio
async def test_scope_skip_leaves_incident_redispatchable(monkeypatch, tmp_path):
    """Скип по области действия не должен запирать инцидент в OPEN.

    OPEN входит в `_SKIP_STATES` вебхука: оставшись там, инцидент
    дедуплицировался бы на каждом следующем fire, и расширение фильтра не
    подхватило бы уже активный алерт, пока тот не погаснет и не загорится
    снова.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.state_machine import IncidentState
    from app.database import Base, IncidentRecord
    from app.workers import tasks

    engine = create_engine(f"sqlite:///{tmp_path}/scope.db")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(tasks, "SessionLocal", Session)

    db = Session()
    db.add(IncidentRecord(
        incident_id="fp-1", status=IncidentState.OPEN.value, data={},
    ))
    db.commit()
    db.close()

    monkeypatch.setattr(settings, "LLM_PIPELINE_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "PIPELINE_SEVERITY_ALLOWLIST", ["critical"], raising=False)
    monkeypatch.setattr(settings, "PIPELINE_NAMESPACE_PREFIXES", [], raising=False)

    await tasks.async_process_incident(
        {"incident_id": "fp-1", "severity": "warning", "namespace": "dev-17"}
    )

    db = Session()
    status = db.query(IncidentRecord).filter_by(incident_id="fp-1").first().status
    db.close()

    assert status == IncidentState.TRIAGE_REQUIRED.value
    assert status != IncidentState.OPEN.value, "инцидент остался бы недостижим для re-fire"
