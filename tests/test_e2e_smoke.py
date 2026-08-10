"""E2E smoke-тест: HTTP webhook → pipeline → DB.

В отличие от scripts/run_e2e_local.sh (требует Claude CLI и mcp-teamcity), здесь
всё мокируется и работает в CI без сетевых обращений. Цель — поймать регрессии
склейки слоёв (router → state machine → trace → repository), а не качество
синтеза LLM.
"""
import hashlib
import hmac
import json
import uuid
from unittest.mock import patch

import pytest

from tests.conftest import requires_postgres

# Вебхук теперь fail-closed: без валидной подписи запрос отклоняется в любом
# ENV. Smoke-тест ходит через РЕАЛЬНЫЙ аутентифицированный путь, а не в обход —
# иначе он перестал бы покрывать связку auth → router → pipeline.
_E2E_SECRET = "e2e-smoke-secret"

# E2E smoke использует реальный `engine` из app.database через
# `Base.metadata.create_all(engine)` + чтение IncidentRecord. На
# self-hosted CI runner'е postgres не запущен (services: блок GitHub
# Actions работает только на ubuntu-latest). Conditional skip — см.
# conftest._has_live_postgres / requires_postgres marker.
pytestmark = requires_postgres


@pytest.fixture(autouse=True)
def _hmac_secret(monkeypatch):
    """Секрет для подписи + чистка anti-replay кэша между тестами."""
    from app.config import settings
    from app.security.replay import alertmanager_signature_cache

    monkeypatch.setattr(settings, "ALERTMANAGER_WEBHOOK_SECRET", _E2E_SECRET)
    alertmanager_signature_cache.clear()
    yield
    alertmanager_signature_cache.clear()


def _post_signed(client, url: str, payload: dict):
    """POST с корректной HMAC-подписью тела.

    Сериализуем сами: подпись обязана считаться над ТЕМИ ЖЕ байтами, что уйдут
    по сети (повторный json.dumps внутри клиента дал бы другие байты).
    """
    body = json.dumps(payload).encode()
    sig = hmac.new(_E2E_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        url,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Alertmanager-Signature": sig,
        },
    )


@pytest.fixture(scope="module")
def app_client():
    """TestClient без `with` + PIPELINE_DIRECT_INVOKE patch.

    Два важных нюанса:
      1. pydantic Settings парсятся один раз при первом импорте app.config;
         если другой тест импортировал раньше — env-var уже бесполезны.
         Поэтому патчим settings.PIPELINE_DIRECT_INVOKE напрямую — иначе
         webhook улетит в Celery .delay() и потащит за собой Redis-connection
         retry-loop, отравляющий все последующие тесты.
      2. `with TestClient(app)` триггерит shutdown_event, который зовёт
         `celery_app.control.shutdown()` → kombu connect к несуществующему
         Redis → exception в teardown. Используем без context manager.
    """
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.database import Base, engine
    from app.main import app

    Base.metadata.create_all(engine)
    with patch.object(settings, "PIPELINE_DIRECT_INVOKE", True):
        yield TestClient(app)


@pytest.fixture
def mock_llm():
    """Все 6 агентов отвечают валидным JSON / текстом — pipeline проходит."""
    async def fake_ask(self, user_context, instruction=""):
        # AnalyzerAgent ожидает JSON с confidence_score >= 0.7 для перехода.
        if self.name == "Analyzer":
            return '{"confidence_score": 0.9, "analysis": "stub"}'
        # Остальные агенты возвращают plain text — он не парсится JSON-ом.
        return f"STUB-RESPONSE-from-{self.name}"

    with patch("app.agents.base.BaseAgent.ask", new=fake_ask):
        yield


@pytest.fixture
def mock_k8s_context():
    """ContextBuilder не лезет в реальный k8s."""
    async def _stub(self, incident):
        return {
            "incident": incident,
            "metrics": None,
            "deployments": [],
            "logs_summary": "stub",
        }

    with patch("app.context.context_builder.ContextBuilder.__init__", return_value=None), \
         patch("app.context.context_builder.ContextBuilder.build_context", new=_stub):
        yield


@pytest.fixture
def mock_teamcity():
    """TC-контекст всегда None — best-effort enrichment."""
    async def _none(*a, **kw):
        return None

    with patch("app.api.webhooks.incident_teamcity_context", new=_none):
        yield


@pytest.fixture
def mock_discord_delivery():
    """Отчёт «доставлен» — тест про incident record, а не про Discord.

    conftest подставляет DISCORD_WEBHOOK_URL=https://example.com/..., поэтому
    без мока POST реально уходит и возвращает 405. С outbox-семантикой
    доставки (кодревью 10.08.2026) это уже не глотается: delivered=False →
    pipeline поднимает ReportDeliveryPending, чтобы Celery переотправил
    отчёт. Тест падал именно на этом, хотя проверяет создание записи
    инцидента. Такой же мок стоит в остальных тестах, гоняющих пайплайн
    целиком (test_pipeline_trace, test_state_transitions, test_replay_fixtures).
    """
    async def _delivered(*a, **kw):
        return True

    with patch(
        "app.services.discord_service.discord_service.send_incident_report",
        new=_delivered,
    ):
        yield


def test_webhook_creates_incident_record(
    app_client, mock_llm, mock_k8s_context, mock_teamcity, mock_discord_delivery
):
    # Уникальный fingerprint на каждый прогон — иначе при повторе теста (или
    # на dev-машине с локальным sre.db) попадётся существующая запись со
    # status=RESOLVED и pipeline упадёт на transition RESOLVED → INVESTIGATING.
    fingerprint = f"e2e-smoke-{uuid.uuid4().hex[:12]}"
    payload = {
        "version": "4",
        "groupKey": "e2e-smoke",
        "status": "firing",
        "receiver": "sre-copilot",
        "groupLabels": {"alertname": "PodCrashLooping"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "https://alertmanager.local",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "PodCrashLooping",
                    "severity": "critical",
                    "namespace": "squad-1",
                    "pod": "stub-pod-1",
                },
                "annotations": {
                    "summary": "stub-pod-1 CrashLoopBackOff",
                    "description": "exit code 137",
                },
                "startsAt": "2026-05-12T10:00:00Z",
                "endsAt": None,
                "generatorURL": "https://prometheus.local",
                "fingerprint": fingerprint,
            }
        ],
    }
    resp = _post_signed(app_client, "/webhooks/alertmanager", payload)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["alerts"][0]["incident_id"] == fingerprint

    # Проверяем, что инцидент реально лёг в БД с trace-ом.
    from app.database import IncidentRecord, SessionLocal

    with SessionLocal() as db:
        rec = (
            db.query(IncidentRecord)
            .filter(IncidentRecord.incident_id == fingerprint)
            .one()
        )
        # Допустимые статусы берём из state machine, а не списком литералов:
        # терминальные (переходов дальше нет) плюс два промежуточных, на
        # которых прогон может застать пайплайн. Хардкод пропускал
        # TRIAGE_REQUIRED — штатный терминал «кандидата на фикс нет», и
        # ровно в него уходит прогон без доступного LLM.
        from app.core.state_machine import IncidentState, StateMachine

        terminal = {
            s.value
            for s, nxt in StateMachine.TRANSITIONS.items()
            if not nxt
        }
        acceptable = terminal | {
            IncidentState.HYPOTHESIS_GENERATED.value,
            IncidentState.FIX_PROPOSED.value,
        }
        assert rec.status in acceptable, (
            f"unexpected terminal status: {rec.status} (ожидались {sorted(acceptable)})"
        )
        # trace может быть None если pipeline упал на ранней стадии — но в
        # тестовом mock-е все 6 стадий проходят, проверяем что что-то есть.
        if rec.trace:
            assert isinstance(rec.trace, list)
            assert len(rec.trace) >= 1


