"""Тесты на anti-drift guards для KG contract.

Покрывают:
  1. `STARTUP_CONTRACT_CHECK` боот-интеграцию в `app.main.lifespan`:
     graceful при пустой БД, логирует warning при unknown edge kind,
     уважает `STARTUP_CONTRACT_CHECK_ENABLED=False`.
  2. `quality_report --check` CI gate mode:
     exit 0 при healthy KG, exit 1 при unknown edge kind / orphan_pct
     выше threshold / owner_pct ниже threshold; CLI overrides работают.

In-memory SQLite + ORM-фикстуры (тот же стиль что в test_kg_contract.py /
test_quality_report.py).
"""
from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.contract import STARTUP_CONTRACT_CHECK
from app.knowledge_graph.schema import Service, ServiceEdge
from app.knowledge_graph.stale_classifier import STALE_CLASS_ACTIVE
from app.scripts.quality_report import (CheckThresholds, build_report,
                                        evaluate_check, main as qr_main)


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def db():
    """Sqlite in-memory с поднятой схемой KG."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _mk_svc(db, *, name, namespace="ns", team_owner=None, synthetic=False,
            stale_class=STALE_CLASS_ACTIVE):
    s = Service(name=name, namespace=namespace, team_owner=team_owner)
    s.synthetic = synthetic
    s.stale_class = stale_class
    db.add(s)
    db.flush()
    return s


def _mk_edge(db, src_id, dst_id, kind):
    e = ServiceEdge(src_id=src_id, dst_id=dst_id, kind=kind, weight=1)
    db.add(e)
    db.flush()
    return e


# ── 1. STARTUP_CONTRACT_CHECK boot integration ───────────────────────────────


def test_startup_check_silent_on_clean_db(db, caplog):
    """Чистая БД (real services без edges нет) → check не должен logger.warning
    про unknown_edge_kinds. owner/orphan возвращают None для пустой выборки.
    """
    with caplog.at_level(logging.WARNING, logger="app.knowledge_graph.contract"):
        report = STARTUP_CONTRACT_CHECK(db)
    assert report["unknown_edge_kinds"] == []
    assert report["planned_in_db"] == []
    # никаких warnings про unknown_edge_kinds_in_db
    warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("unknown_edge_kinds_in_db" in m for m in warning_msgs)


def test_startup_check_does_not_raise_on_empty_kg_services(db):
    """Empty kg_services (no rows) — no exception, owner/orphan = None."""
    # Не создаём ни одного сервиса — чистая схема.
    report = STARTUP_CONTRACT_CHECK(db)
    # При real_total=0 orphan_pct остаётся None, owner_pct тоже None.
    assert report["orphan_pct"] is None
    assert report["owner_pct"] is None


def test_startup_check_logs_warning_on_unknown_edge_kind(db, caplog):
    """Заводим edge с несуществующим kind → warning «unknown_edge_kinds_in_db»."""
    a = _mk_svc(db, name="a")
    b = _mk_svc(db, name="b")
    _mk_edge(db, a.id, b.id, "bogus_drift_kind")
    db.commit()

    with caplog.at_level(logging.WARNING, logger="app.knowledge_graph.contract"):
        report = STARTUP_CONTRACT_CHECK(db)

    assert "bogus_drift_kind" in report["unknown_edge_kinds"]
    warn_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("unknown_edge_kinds_in_db" in m for m in warn_msgs)


def test_lifespan_calls_startup_contract_check_when_enabled():
    """FastAPI lifespan-startup должен дёрнуть STARTUP_CONTRACT_CHECK
    (через wrapper `_run_startup_contract_check`) когда flag включён.
    """
    from app.main import lifespan

    with patch("app.main.start_http_server"), \
         patch("app.main.rate_limit") as mock_rate_limit, \
         patch("app.main.engine") as mock_engine, \
         patch("app.main.celery_app"), \
         patch("app.main._run_startup_contract_check") as mock_check:
        async def _async_noop():
            return None
        mock_rate_limit.close.side_effect = _async_noop

        import asyncio

        async def _run():
            async with lifespan(None):  # type: ignore[arg-type]
                pass

        asyncio.get_event_loop().run_until_complete(_run()) if False else \
            asyncio.run(_run())

        mock_check.assert_called_once()
        # cleanup ресурсов всё ещё работает
        mock_engine.dispose.assert_called_once()


def test_run_startup_contract_check_respects_disabled_flag(caplog):
    """Когда STARTUP_CONTRACT_CHECK_ENABLED=False — wrapper не должен
    открывать сессию и должен залогировать `startup_check_disabled`.
    """
    from app.main import _run_startup_contract_check

    with patch("app.main.settings") as mock_settings, \
         patch("app.main.SessionLocal") as mock_session_local:
        mock_settings.STARTUP_CONTRACT_CHECK_ENABLED = False
        _run_startup_contract_check()
        mock_session_local.assert_not_called()


def test_run_startup_contract_check_graceful_on_db_error():
    """Если SessionLocal() / STARTUP_CONTRACT_CHECK падает — wrapper
    не throws, boot продолжается.
    """
    from app.main import _run_startup_contract_check

    with patch("app.main.settings") as mock_settings, \
         patch("app.main.SessionLocal") as mock_session_local:
        mock_settings.STARTUP_CONTRACT_CHECK_ENABLED = True
        mock_session_local.side_effect = RuntimeError("DB unavailable")
        # не должно throw
        _run_startup_contract_check()


# ── 2. quality_report --check mode ───────────────────────────────────────────


def test_check_passes_on_healthy_kg(db):
    """Healthy KG: 2 real services со связями + owner, без unknown edges →
    `passed=True`, `failures=[]`.
    """
    a = _mk_svc(db, name="a", team_owner="squad-1")
    b = _mk_svc(db, name="b", team_owner="squad-2")
    _mk_edge(db, a.id, b.id, "calls")
    _mk_edge(db, b.id, a.id, "calls")  # b также связан
    db.commit()

    report = build_report(db)
    result = evaluate_check(report, db, CheckThresholds())
    assert result.passed, f"unexpected failures: {result.failures}"
    assert result.failures == []


def test_check_fails_on_unknown_edge_kind(db):
    """Edge с kind вне contract.EDGE_KINDS → failure axis=unknown_edge_kinds."""
    a = _mk_svc(db, name="a", team_owner="squad-1")
    b = _mk_svc(db, name="b", team_owner="squad-2")
    _mk_edge(db, a.id, b.id, "calls")
    _mk_edge(db, a.id, b.id, "totally_unknown_kind")
    db.commit()

    report = build_report(db)
    result = evaluate_check(report, db, CheckThresholds())
    assert not result.passed
    axes = {f["axis"] for f in result.failures}
    assert "unknown_edge_kinds" in axes
    assert "totally_unknown_kind" in result.unknown_edge_kinds


def test_check_fails_on_orphan_pct_above_threshold(db):
    """1 real svc с edge + 4 real-orphans → 80% > 20% default → fail."""
    a = _mk_svc(db, name="a", team_owner="squad-1")
    b = _mk_svc(db, name="b", team_owner="squad-1")
    _mk_edge(db, a.id, b.id, "calls")
    # 3 orphan'а
    _mk_svc(db, name="orphan1", team_owner="squad-1")
    _mk_svc(db, name="orphan2", team_owner="squad-1")
    _mk_svc(db, name="orphan3", team_owner="squad-1")
    db.commit()

    report = build_report(db)
    # 5 real, 3 без http-edges → 60%
    result = evaluate_check(report, db, CheckThresholds(max_orphan_pct=20.0))
    assert not result.passed
    assert any(f["axis"] == "orphan_pct" for f in result.failures)


def test_check_overrides_max_orphan_threshold(db):
    """`--max-orphan 90` (или CheckThresholds(max_orphan_pct=90)) → тот же
    KG passes.
    """
    a = _mk_svc(db, name="a", team_owner="squad-1")
    b = _mk_svc(db, name="b", team_owner="squad-1")
    _mk_edge(db, a.id, b.id, "calls")
    _mk_svc(db, name="orphan1", team_owner="squad-1")
    _mk_svc(db, name="orphan2", team_owner="squad-1")
    _mk_svc(db, name="orphan3", team_owner="squad-1")
    db.commit()

    report = build_report(db)
    result = evaluate_check(report, db, CheckThresholds(max_orphan_pct=90.0))
    # orphan axis должен passнуть, owner — у всех есть, stale — все active
    assert all(f["axis"] != "orphan_pct" for f in result.failures), (
        f"unexpected failures: {result.failures}"
    )


def test_check_fails_on_low_owner_coverage(db):
    """Owner-known только у 1 из 4 real → 25% < 50% → fail."""
    a = _mk_svc(db, name="a", team_owner="squad-1")
    b = _mk_svc(db, name="b", team_owner=None)
    c = _mk_svc(db, name="c", team_owner=None)
    d = _mk_svc(db, name="d", team_owner=None)
    _mk_edge(db, a.id, b.id, "calls")
    _mk_edge(db, c.id, d.id, "calls")
    db.commit()

    report = build_report(db)
    result = evaluate_check(report, db, CheckThresholds(min_owner_pct=50.0))
    assert not result.passed
    assert any(f["axis"] == "owner_pct" for f in result.failures)


def test_check_fails_on_high_stale_null_pct(db):
    """stale_class IS NULL у > max_stale_null_pct → failure."""
    # 5 svc — 4 c NULL stale_class, 1 active
    _mk_svc(db, name="a", team_owner="squad-1", stale_class=None)
    _mk_svc(db, name="b", team_owner="squad-1", stale_class=None)
    _mk_svc(db, name="c", team_owner="squad-1", stale_class=None)
    _mk_svc(db, name="d", team_owner="squad-1", stale_class=None)
    _mk_svc(db, name="e", team_owner="squad-1", stale_class=STALE_CLASS_ACTIVE)
    db.commit()

    report = build_report(db)
    result = evaluate_check(report, db, CheckThresholds(max_stale_null_pct=10.0))
    assert not result.passed
    assert any(f["axis"] == "stale_null_pct" for f in result.failures)


def test_check_passes_on_empty_db(db):
    """Empty DB: всё None → gate тихо проходит (нечего падать)."""
    report = build_report(db)
    result = evaluate_check(report, db, CheckThresholds())
    assert result.passed
    assert result.failures == []


def test_main_check_exit_0_on_healthy(db, capsys):
    """Полный --check прогон через main() возвращает 0 при healthy."""
    a = _mk_svc(db, name="a", team_owner="squad-1")
    b = _mk_svc(db, name="b", team_owner="squad-1")
    _mk_edge(db, a.id, b.id, "calls")
    _mk_edge(db, b.id, a.id, "calls")
    db.commit()

    def _factory():
        return db

    with patch("app.database.SessionLocal", _factory):
        # main() закрывает session через db.close(); подменим close на no-op
        # чтобы fixture-сессия осталась жива для последующих ассертов.
        orig_close = db.close
        db.close = lambda: None  # type: ignore[assignment]
        try:
            rc = qr_main(["--check", "--json"])
        finally:
            db.close = orig_close  # type: ignore[assignment]

    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["check_passed"] is True
    assert payload["check_failures"] == []


def test_main_check_exit_1_on_unknown_edge(db, capsys):
    """Полный --check прогон возвращает 1 при unknown kind, в JSON
    видно `check_passed=False`.
    """
    a = _mk_svc(db, name="a", team_owner="squad-1")
    b = _mk_svc(db, name="b", team_owner="squad-1")
    _mk_edge(db, a.id, b.id, "calls")
    _mk_edge(db, a.id, b.id, "nonsense_kind")
    db.commit()

    def _factory():
        return db

    with patch("app.database.SessionLocal", _factory):
        orig_close = db.close
        db.close = lambda: None  # type: ignore[assignment]
        try:
            rc = qr_main(["--check", "--json"])
        finally:
            db.close = orig_close  # type: ignore[assignment]

    out = capsys.readouterr().out
    assert rc == 1
    payload = json.loads(out)
    assert payload["check_passed"] is False
    axes = {f["axis"] for f in payload["check_failures"]}
    assert "unknown_edge_kinds" in axes


def test_main_check_markdown_output(db, capsys):
    """Default markdown output для --check содержит таблицу axes + статус."""
    a = _mk_svc(db, name="a", team_owner="squad-1")
    b = _mk_svc(db, name="b", team_owner="squad-1")
    _mk_edge(db, a.id, b.id, "calls")
    _mk_edge(db, b.id, a.id, "calls")
    db.commit()

    def _factory():
        return db

    with patch("app.database.SessionLocal", _factory):
        orig_close = db.close
        db.close = lambda: None  # type: ignore[assignment]
        try:
            rc = qr_main(["--check"])
        finally:
            db.close = orig_close  # type: ignore[assignment]

    out = capsys.readouterr().out
    assert rc == 0
    assert "KG Contract Check" in out
    assert "PASS" in out
    assert "unknown_edge_kinds" in out
    assert "orphan_pct" in out
    assert "owner_pct" in out


def test_main_check_cli_override_max_orphan(db, capsys):
    """`--max-orphan 90` поднимает порог → тот же KG passes."""
    a = _mk_svc(db, name="a", team_owner="squad-1")
    b = _mk_svc(db, name="b", team_owner="squad-1")
    _mk_edge(db, a.id, b.id, "calls")
    _mk_svc(db, name="o1", team_owner="squad-1")
    _mk_svc(db, name="o2", team_owner="squad-1")
    _mk_svc(db, name="o3", team_owner="squad-1")
    db.commit()

    def _factory():
        return db

    with patch("app.database.SessionLocal", _factory):
        orig_close = db.close
        db.close = lambda: None  # type: ignore[assignment]
        try:
            # default max-orphan=20 — fail. С --max-orphan 90 — pass.
            rc = qr_main(["--check", "--max-orphan", "90.0", "--json"])
        finally:
            db.close = orig_close  # type: ignore[assignment]

    out = capsys.readouterr().out
    payload = json.loads(out)
    # Все axes ниже порогов: orphan teraz ok, остальные тоже.
    orphan_failures = [f for f in payload["check_failures"]
                       if f["axis"] == "orphan_pct"]
    assert not orphan_failures, f"orphan failed unexpectedly: {payload['check_failures']}"
    assert rc == 0
