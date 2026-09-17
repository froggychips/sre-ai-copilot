"""Область действия пайплайна: что доходит до LLM, а что нет.

Третье условие включения `LLM_PIPELINE_ENABLED` — «severity-фильтр сужен
до critical + prod-*». Проверяется и сам фильтр, и то, что он стоит на
пути, мимо которого не пройти.
"""
from unittest.mock import MagicMock, patch

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
async def test_scope_gate_blocks_pipeline_entry(monkeypatch, tmp_path):
    """Фильтр стоит на входе в задачу — мимо него не пройти ни одним путём."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base
    from app.workers import tasks

    # Своя БД: на пути скипа задача убирает осиротевшую запись, и без
    # подмены тест лез бы в настоящий Postgres. Транзиентную ошибку такого
    # обращения уборка намеренно пробрасывает — see
    # test_transient_cleanup_failure_is_retried.
    engine = create_engine(f"sqlite:///{tmp_path}/gate.db")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(tasks, "SessionLocal", sessionmaker(bind=engine))

    monkeypatch.setattr(settings, "LLM_PIPELINE_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "PIPELINE_SEVERITY_ALLOWLIST", ["critical"], raising=False)
    monkeypatch.setattr(settings, "PIPELINE_NAMESPACE_PREFIXES", ["prod-"], raising=False)

    result = await tasks.async_process_incident(
        {"incident_id": "x", "severity": "warning", "namespace": "dev-17"}
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "severity_out_of_scope"


# --- фильтр в вебхуке: запись не создаётся вовсе ---------------------------

def _webhook_payload(severity: str, namespace: str, status: str = "firing") -> dict:
    return {
        "version": "4",
        "groupKey": "g1",
        "status": status,
        "alerts": [{
            "status": status,
            "labels": {"severity": severity, "namespace": namespace,
                       "alertname": "TestAlert"},
            "annotations": {"summary": "тест"},
            "startsAt": "2026-09-17T10:00:00Z",
            "fingerprint": "fp-scope-1",
        }],
    }


@pytest.fixture
def scoped_db(monkeypatch, tmp_path):
    """Живая sqlite-БД + продовый scope-фильтр."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base

    engine = create_engine(f"sqlite:///{tmp_path}/scope.db")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    monkeypatch.setattr(settings, "PIPELINE_SEVERITY_ALLOWLIST", ["critical"], raising=False)
    monkeypatch.setattr(settings, "PIPELINE_NAMESPACE_PREFIXES", ["prod-"], raising=False)
    return Session


@pytest.mark.asyncio
async def test_out_of_scope_alert_creates_no_record(scoped_db):
    """Отфильтрованный алерт не оставляет строки в БД.

    Запись, созданная для алерта, который пайплайн разбирать не будет,
    дальше мешает трижды: OPEN входит в `_SKIP_STATES` и глушит дедупом
    последующие fire; терминальный статус делает резолв no-op'ом; re-fire
    при `repeat_interval` засчитывается флаппингом. Отсутствие записи
    снимает все три разом.
    """
    from app.api.webhooks import alertmanager_webhook
    from app.database import IncidentRecord
    from app.models.incident import AlertManagerWebhook

    db = scoped_db()
    payload = AlertManagerWebhook(**_webhook_payload("warning", "dev-17"))

    result = await alertmanager_webhook(payload, db=db)

    assert result["alerts"][0]["task_id"] == "out_of_scope"
    assert db.query(IncidentRecord).count() == 0, "строки быть не должно"
    db.close()


@pytest.mark.asyncio
async def test_repeat_firing_does_not_accumulate_flaps(scoped_db):
    """Повтор firing по repeat_interval не накручивает flap_count.

    AlertManager шлёт уведомление снова, пока алерт горит. Записи нет —
    считать нечего, и ложной истории флаппинга не появляется.
    """
    from app.api.webhooks import alertmanager_webhook
    from app.database import IncidentRecord
    from app.models.incident import AlertManagerWebhook

    db = scoped_db()
    payload = AlertManagerWebhook(**_webhook_payload("warning", "dev-17"))

    for _ in range(3):
        await alertmanager_webhook(payload, db=db)

    assert db.query(IncidentRecord).count() == 0
    db.close()


@pytest.mark.asyncio
async def test_widened_scope_picks_up_the_next_firing(scoped_db, monkeypatch):
    """Расширили фильтр — следующий firing обрабатывается, ждать резолва не нужно."""
    from app.api.webhooks import alertmanager_webhook
    from app.database import IncidentRecord
    from app.models.incident import AlertManagerWebhook

    db = scoped_db()
    payload = AlertManagerWebhook(**_webhook_payload("warning", "dev-17"))
    await alertmanager_webhook(payload, db=db)
    assert db.query(IncidentRecord).count() == 0

    monkeypatch.setattr(settings, "PIPELINE_SEVERITY_ALLOWLIST", [], raising=False)
    monkeypatch.setattr(settings, "PIPELINE_NAMESPACE_PREFIXES", [], raising=False)
    with patch("app.api.webhooks.process_incident_task") as task:
        task.delay.return_value = MagicMock(id="t1")
        await alertmanager_webhook(payload, db=db)

    assert db.query(IncidentRecord).count() == 1
    db.close()


@pytest.mark.asyncio
async def test_resolve_of_existing_record_still_applies(scoped_db):
    """Резолв записи, созданной когда фильтр был шире, обязан отработать.

    Поэтому scope-проверка стоит ПОСЛЕ ветки resolved: иначе инцидент,
    принятый до сужения фильтра, навсегда остался бы незакрытым.
    """
    from app.api.webhooks import alertmanager_webhook
    from app.core.state_machine import IncidentState
    from app.database import IncidentRecord
    from app.models.incident import AlertManagerWebhook

    db = scoped_db()
    db.add(IncidentRecord(
        incident_id="fp-scope-1", status=IncidentState.OPEN.value, data={},
    ))
    db.commit()

    payload = AlertManagerWebhook(**_webhook_payload("warning", "dev-17", status="resolved"))
    await alertmanager_webhook(payload, db=db)

    row = db.query(IncidentRecord).filter_by(incident_id="fp-scope-1").first()
    assert row.status == IncidentState.RESOLVED.value
    db.close()


@pytest.mark.asyncio
async def test_in_scope_alert_is_accepted(scoped_db):
    """Продовый critical проходит фильтр и попадает в обработку."""
    from app.api.webhooks import alertmanager_webhook
    from app.database import IncidentRecord
    from app.models.incident import AlertManagerWebhook

    db = scoped_db()
    payload = AlertManagerWebhook(**_webhook_payload("critical", "prod-k5"))

    with patch("app.api.webhooks.process_incident_task") as task:
        task.delay.return_value = MagicMock(id="t1")
        result = await alertmanager_webhook(payload, db=db)

    assert result["alerts"][0]["task_id"] != "out_of_scope"
    assert db.query(IncidentRecord).count() == 1
    db.close()


# --- рассинхрон api и worker в окне выкатки --------------------------------


# --- решение об области принимается один раз -------------------------------

@pytest.mark.asyncio
async def test_webhook_decision_travels_with_the_task(scoped_db):
    """Вебхук помечает задачу проверенной — воркер не решает заново.

    api и worker — разные деплойменты, и deploy.sh обновляет их по очереди:
    в окне выкатки их настройки области расходятся. Два независимых ответа
    на один вопрос означали бы, что запись создана по одному решению, а
    обработана по другому, — и тогда строка остаётся в БД без обработчика.
    """
    from app.api.webhooks import alertmanager_webhook
    from app.models.incident import AlertManagerWebhook
    from app.workers.pipeline_scope import SCOPE_APPROVED_KEY

    db = scoped_db()
    payload = AlertManagerWebhook(**_webhook_payload("critical", "prod-k5"))

    with patch("app.api.webhooks.process_incident_task") as task:
        task.delay.return_value = MagicMock(id="t1")
        await alertmanager_webhook(payload, db=db)

    dispatched = task.delay.call_args.args[0]
    assert dispatched[SCOPE_APPROVED_KEY] is True
    db.close()


@pytest.mark.asyncio
async def test_approved_task_is_not_rechecked(monkeypatch):
    """Задача с меткой проходит, даже если настройки воркера уже. """
    from app.workers import tasks
    from app.workers.pipeline_scope import SCOPE_APPROVED_KEY

    monkeypatch.setattr(settings, "LLM_PIPELINE_ENABLED", True, raising=False)
    # У воркера область уже некуда: без метки алерт был бы отвергнут.
    monkeypatch.setattr(settings, "PIPELINE_SEVERITY_ALLOWLIST", ["critical"], raising=False)
    monkeypatch.setattr(settings, "PIPELINE_NAMESPACE_PREFIXES", ["prod-"], raising=False)

    checked = []
    monkeypatch.setattr(tasks, "check_scope", lambda d: checked.append(d) or None)

    with patch.object(tasks, "SessionLocal", side_effect=RuntimeError("дальше не идём")):
        with pytest.raises(RuntimeError):
            await tasks.async_process_incident({
                "incident_id": "fp-approved", "severity": "warning",
                "namespace": "dev-17", SCOPE_APPROVED_KEY: True,
            })

    assert checked == [], "решение вебхука не должно перепроверяться"


@pytest.mark.asyncio
async def test_unmarked_task_is_still_checked(monkeypatch):
    """Прямой вызов без метки проверяется: вебхука в пути не было."""
    from app.workers import tasks

    monkeypatch.setattr(settings, "LLM_PIPELINE_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "PIPELINE_SEVERITY_ALLOWLIST", ["critical"], raising=False)
    monkeypatch.setattr(settings, "PIPELINE_NAMESPACE_PREFIXES", [], raising=False)

    result = await tasks.async_process_incident(
        {"incident_id": "fp-direct", "severity": "warning", "namespace": "dev-17"}
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "severity_out_of_scope"
