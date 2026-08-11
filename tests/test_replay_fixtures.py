"""Replay-snapshot fixtures: regression-проверка pipeline'а на 7 типовых
WO incident-сценариях БЕЗ единого LLM-вызова.

## Что покрываем

7 fixtures в `tests/fixtures/incidents/*.json` — реалистичные AlertManager
payloads под разные классы инцидентов:
  - crashloop          CrashLoopBackOff (exit 137)
  - oomkilled          OOMKilled (explicit reason)
  - imagepull          ImagePullBackOff (после deploy)
  - highlatency        HighLatency (p99 > threshold)
  - diconfig           DI / IStatics misconfig — WO-специфичный
  - nodedisk           NodeDiskIOSaturation — node-level, не service
  - scrapenotargets    ScrapePoolHasNoTargets — observability config

## Cost-tracking invariants

Тест гарантирует что **0 реальных LLM-вызовов** делается:
  - `ModelRouter.route_and_call_full` запатчен `raise AssertionError` —
    если test случайно проскочит мимо mocks и дёрнет реальный route,
    упадёт с явным сообщением.
  - На уровне stage-агентов: `analyze`/`generate`/`critique_all`/etc.
    замокированы AsyncMock — счётчик вызовов проверяется явно.

Эти fixtures **разблокируют Stage 3**: можно регрессионно прогонять
pipeline на каждый новый prompt change без LLM-burn ($0 за прогон).

## Как добавлять новый сценарий

  1. tests/fixtures/incidents/<name>.json — следуй существующей форме
     (_fixture_id, _description обязательны для self-doc).
  2. Добавь имя в `FIXTURE_NAMES`. parametrize-collection подхватит.

## Валидатор снапшотов (app/snapshot/validator.py)

Второй блок в конце файла — контракт `validate_snapshot`: снапшот и есть
единственный вход в replay (`POST /replay/{incident_id}` → Celery), поэтому
проверяется, что подменённый материал ловится, а часы источника не роняют
каждый снапшот в DEGRADED.
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.models.hypothesis import Hypothesis, HypothesisSet
from app.core.state_machine import IncidentState
from app.diagnostics.facts import Fact, FactKind, FactStore
from app.snapshot.schema import (
    build_snapshot_from_incident,
    compute_log_window_hash,
    compute_metric_snapshot_hash,
    compute_topology_hash,
)
from app.snapshot.validator import validate_snapshot, validate_snapshot_model

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "incidents"

FIXTURE_NAMES = [
    "crashloop",
    "oomkilled",
    "imagepull",
    "highlatency",
    "diconfig",
    "nodedisk",
    "scrapenotargets",
]


def _load_fixture(name: str) -> dict:
    """Загрузить JSON fixture и убрать meta-поля (_fixture_id, _description)."""
    path = _FIXTURE_DIR / f"{name}.json"
    data = json.loads(path.read_text())
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _make_hypothesis_set() -> HypothesisSet:
    """Minimal valid hypothesis set — pipeline проходит fact-critic фильтр."""
    return HypothesisSet(items=[
        Hypothesis(
            cause="stub-hypothesis",
            anchored_facts=[FactKind.OOM_KILLED],
            confidence=0.9,
            perspective="infra",
        ),
    ])


def _make_fact_store() -> FactStore:
    """FactStore с одним observed fact (anchor для hypothesis выше)."""
    return FactStore([
        Fact(kind=FactKind.OOM_KILLED, observed=True, confidence=0.95),
    ])


@pytest.fixture
def mocked_pipeline_deps(mocker):
    """Mock все LLM-агенты + БД + Discord + audit + ModelRouter."""
    # Stage agents (LLM-bound)
    analyze_mock = mocker.patch(
        "app.workers.pipeline.AnalyzerAgent.analyze",
        new_callable=AsyncMock, return_value="stub-analysis",
    )
    fix_mock = mocker.patch(
        "app.workers.pipeline.FixAgent.suggest",
        new_callable=AsyncMock, return_value=("stub-fix", None),
    )
    risk_mock = mocker.patch(
        "app.workers.pipeline.RiskAgent.assess",
        new_callable=AsyncMock, return_value="stub-risk",
    )
    synth_mock = mocker.patch(
        "app.workers.pipeline.SynthesisAgent.synthesize",
        new_callable=AsyncMock, return_value="stub-synth",
    )
    hyp_mock = mocker.patch(
        "app.workers.pipeline.MultiHypothesisAgent.generate",
        new_callable=AsyncMock, return_value=_make_hypothesis_set(),
    )
    critic_mock = mocker.patch(
        "app.workers.pipeline.FactCriticAgent.critique_all",
        new_callable=AsyncMock, return_value=_make_hypothesis_set(),
    )

    # Non-LLM dependencies
    mocker.patch(
        "app.workers.pipeline.diag_engine.run",
        return_value=_make_fact_store(),
    )
    mocker.patch(
        "app.workers.pipeline.SimilarIncidentEngine.find",
        return_value=[],
    )
    mocker.patch(
        "app.workers.pipeline.discord_service.send_report",
        new_callable=AsyncMock,
    )
    # Доставка отчёта отдаёт delivered — мокаем, иначе живой POST в
    # example.com (conftest) даёт False и pipeline уходит в outbox-ретрай.
    mocker.patch(
        "app.workers.pipeline.discord_service.send_incident_report",
        new_callable=AsyncMock,
        return_value=True,
    )
    mocker.patch("app.workers.pipeline.audit_service.log_event")

    # ── COST-TRACKING TRIPWIRE ────────────────────────────────────────
    # Любой проскальзывающий LLM-вызов (через ModelRouter.route_and_call_full
    # или прямой generate_full) должен упасть с явным сообщением.
    # Это страховка от случайного $0.05/incident burn в тестах.
    def _route_tripwire(*args, **kwargs):
        raise AssertionError(
            "Replay-fixture тест попытался сделать РЕАЛЬНЫЙ LLM-вызов через "
            "ModelRouter. Проверь mocked_pipeline_deps — какой-то агент не "
            "замокан. Args: %r kwargs: %r" % (args, kwargs)
        )
    mocker.patch(
        "app.llm.router.ModelRouter.route_and_call_full",
        side_effect=_route_tripwire,
    )
    mocker.patch(
        "app.llm.router.ModelRouter.route_and_call",
        side_effect=_route_tripwire,
    )

    # Mock DB session
    record = MagicMock()
    record.trace = None
    record.analysis = None
    record.status = IncidentState.OPEN.value
    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.first.return_value = record
    mocker.patch("app.workers.tasks.SessionLocal", return_value=mock_session)

    return {
        "record": record,
        "session": mock_session,
        "agent_mocks": {
            "analyze": analyze_mock, "fix": fix_mock, "risk": risk_mock,
            "synth": synth_mock, "hyp": hyp_mock, "critic": critic_mock,
        },
    }


# ── Main parametrized test ─────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
async def test_pipeline_runs_through_fixture_without_real_llm(
    fixture_name, mocked_pipeline_deps,
):
    """Pipeline проходит все 7 стадий на любом fixture'е без exception
    и БЕЗ единого реального LLM-вызова (tripwire не срабатывает).

    Это и есть «replay»: статичный input → детерминированный mock-output
    → проверка что pipeline-плумбинг устойчив к разным incident-классам.
    """
    from app.workers.tasks import async_process_incident

    incident_data = _load_fixture(fixture_name)
    # Pipeline не должен бросить — даже если данные edge-case-овые.
    await async_process_incident(incident_data)

    # Trace записан и содержит ровно 7 стадий.
    record = mocked_pipeline_deps["record"]
    assert record.trace is not None, f"trace отсутствует для {fixture_name}"
    assert len(record.trace) == 7, (
        f"{fixture_name}: ожидалось 7 stages, получили {len(record.trace)}"
    )
    stages = [s["stage"] for s in record.trace]
    assert stages == [
        "analyzer", "diagnostics", "hypothesis", "critic",
        "fix", "risk", "synthesis",
    ], f"{fixture_name}: неверный порядок стадий: {stages}"


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
async def test_pipeline_calls_each_agent_exactly_once(
    fixture_name, mocked_pipeline_deps,
):
    """Каждый stage-агент вызывается ровно один раз (sanity на mock
    plumbing). Если кто-то случайно поставит `for _ in range(3): await
    agent.generate(...)` — этот тест поймает."""
    from app.workers.tasks import async_process_incident

    incident_data = _load_fixture(fixture_name)
    await async_process_incident(incident_data)

    mocks = mocked_pipeline_deps["agent_mocks"]
    for name, mock in mocks.items():
        assert mock.await_count == 1, (
            f"{fixture_name}: agent {name}.await_count = "
            f"{mock.await_count} (ожидалось 1)"
        )


@pytest.mark.asyncio
async def test_fixtures_are_well_formed_and_unique():
    """Sanity-check на сами fixture-файлы.

    - Все 7 файлов существуют и валидный JSON.
    - У каждого есть _fixture_id, _description (self-doc).
    - incident_id уникальны — иначе dedup в pipeline схлопнет тесты в один.
    """
    seen_ids = set()
    for name in FIXTURE_NAMES:
        path = _FIXTURE_DIR / f"{name}.json"
        assert path.exists(), f"fixture {name}.json не существует"
        data = json.loads(path.read_text())
        assert data.get("_fixture_id") == name, (
            f"{name}.json: _fixture_id должен совпадать с именем файла"
        )
        assert data.get("_description"), (
            f"{name}.json: пустой _description — добавь self-doc"
        )
        iid = data["incident_id"]
        assert iid not in seen_ids, f"incident_id {iid} дублируется"
        seen_ids.add(iid)


# ── Валидатор снапшотов: хэши материала ──────────────────────────────────

_PAYLOAD = {
    "targets": [{"id": "inc-1", "kind": "deployment", "name": "town-service"}],
    "policy_name": "wo-default",
    "metrics": {"cpu": 0.9},
    "logs": ["OOMKilled"],
}


def _valid_snapshot(payload: dict | None = None, **overrides) -> dict:
    """Согласованный снапшот: все три хэша посчитаны по payload-у."""
    payload = _PAYLOAD if payload is None else payload
    snap = {
        "snapshot_id": "snap-inc-1-1",
        "incident_id": "inc-1",
        "source_event_ids": ["inc-1"],
        "timestamps": {
            "captured_at": "2026-08-10T10:00:05+00:00",
            "incident_ts": "2026-08-10T10:00:00+00:00",
        },
        "topology_hash": compute_topology_hash(payload),
        "metric_snapshot_hash": compute_metric_snapshot_hash(payload),
        "log_window_hash": compute_log_window_hash(payload),
        "ingest_time_source": "collector-node-clock",
        "correlation_index": [{"event_id": "inc-1", "related_to": ["inc-1"]}],
        "payload": payload,
    }
    snap.update(overrides)
    return snap


def test_intact_snapshot_passes_validation():
    out = validate_snapshot(_valid_snapshot())
    assert out["status"] == "PASS", out
    assert out["confidence_replay_safe"] == "HIGH"
    assert out["warnings"] == []


def test_tampered_topology_targets_are_caught():
    """Подменённые targets в payload → topology_hash не сходится.

    Раньше topology_hash был единственным хэшем, который НЕ пересчитывался:
    материал (targets + policy_name) лежит в payload-е, но проверялись только
    metric/log — рассинхрон топологии уезжал в replay со статусом PASS.
    """
    snap = _valid_snapshot()
    snap["payload"] = {
        **_PAYLOAD,
        "targets": [{"id": "inc-1", "kind": "deployment", "name": "ЧУЖОЙ-service"}],
    }
    out = validate_snapshot(snap)
    assert out["status"] == "FAIL"
    assert out["confidence_replay_safe"] == "LOW"
    assert "topology_hash mismatch" in out["reasons"]
    # Остальные хэши по-прежнему сходятся — ошибка ровно одна, адресная.
    assert out["reasons"] == ["topology_hash mismatch"]


def test_tampered_policy_name_is_caught():
    """policy_name — часть того же материала: подмена политики тоже ловится."""
    snap = _valid_snapshot()
    snap["payload"] = {**_PAYLOAD, "policy_name": "wo-permissive"}
    out = validate_snapshot(snap)
    assert out["status"] == "FAIL"
    assert "topology_hash mismatch" in out["reasons"]


def test_metric_and_log_hash_mismatch_still_caught():
    """Регрессия: перевод трёх хэшей на общие формулы из schema.py не ослабил
    прежние проверки metric/log."""
    snap = _valid_snapshot()
    snap["payload"] = {**_PAYLOAD, "metrics": {"cpu": 0.1}, "logs": []}
    out = validate_snapshot(snap)
    assert out["status"] == "FAIL"
    assert "metric_snapshot_hash mismatch" in out["reasons"]
    assert "log_window_hash mismatch" in out["reasons"]


def test_snapshot_built_from_fixture_validates_pass():
    """Сквозной контракт: то, что собирает build_snapshot_from_incident,
    валидатор считает согласованным (иначе каждый replay = 422)."""
    incident_data = _load_fixture("crashloop")
    snapshot = build_snapshot_from_incident(
        incident_id=str(incident_data["incident_id"]),
        incident_data=incident_data,
        model_version="test-model",
        runtime_version="worker-test",
    )
    out = validate_snapshot_model(snapshot)
    assert out["status"] == "PASS", out


def test_built_snapshot_with_drifted_payload_fails():
    """Тот же снапшот, но payload разъехался после подсчёта хэшей → FAIL."""
    incident_data = _load_fixture("crashloop")
    snapshot = build_snapshot_from_incident(
        incident_id=str(incident_data["incident_id"]),
        incident_data=incident_data,
        model_version="test-model",
        runtime_version="worker-test",
    )
    snapshot.payload = {**snapshot.payload, "targets": [{"id": "smuggled"}]}
    out = validate_snapshot_model(snapshot)
    assert out["status"] == "FAIL"
    assert "topology_hash mismatch" in out["reasons"]


# ── Валидатор снапшотов: допуск на рассинхрон часов ──────────────────────


def _snapshot_with_skew(seconds: float) -> dict:
    """Снапшот, где incident_ts опережает captured_at на `seconds`."""
    from datetime import datetime, timedelta, timezone

    captured = datetime(2026, 8, 10, 10, 0, 0, tzinfo=timezone.utc)
    incident = captured + timedelta(seconds=seconds)
    return _valid_snapshot(
        timestamps={
            "captured_at": captured.isoformat(),
            "incident_ts": incident.isoformat(),
        },
    )


def test_two_second_clock_skew_does_not_degrade():
    """2 секунды NTP-дрейфа AlertManager-а — не сигнал.

    Без допуска строгое `incident_ts > captured_at` отправляло КАЖДЫЙ такой
    снапшот в DEGRADED/MEDIUM, а api/replay.py включал low_fidelity_mode.
    """
    out = validate_snapshot(_snapshot_with_skew(2))
    assert out["status"] == "PASS", out
    assert out["confidence_replay_safe"] == "HIGH"
    assert out["warnings"] == []


def test_clock_skew_at_tolerance_boundary_passes():
    """Ровно на границе допуска (5с) — ещё не warning."""
    out = validate_snapshot(_snapshot_with_skew(5))
    assert out["status"] == "PASS", out


def test_minute_in_the_future_still_warns():
    """Минута «в будущем» — уже не дрейф часов: DEGRADED сохраняется."""
    out = validate_snapshot(_snapshot_with_skew(60))
    assert out["status"] == "DEGRADED"
    assert out["confidence_replay_safe"] == "MEDIUM"
    assert len(out["warnings"]) == 1
    assert "incident_ts is after captured_at" in out["warnings"][0]


def test_normal_order_of_timestamps_never_warns():
    """incident_ts раньше captured_at — семантически нормальный порядок."""
    out = validate_snapshot(_snapshot_with_skew(-600))
    assert out["status"] == "PASS"
    assert out["warnings"] == []
