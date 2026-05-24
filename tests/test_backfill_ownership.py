"""Тесты `app/scripts/backfill_ownership.py`.

Покрытие:
  - Dry-run возвращает correct список, ничего не пишет в БД.
  - Apply применяет UPDATE-ы для team_owner + metadata_json.owner_source.
  - Идемпотентность: повторный apply на тех же данных = 0 updates.
  - Threshold filter: confidence < threshold → skipped.
  - Manual override (confidence=1.0) побеждает heuristic threshold.
  - Limit ограничивает batch size.
  - filter-ns glob работает.
  - --stale flag backfill-ит stale_class (несколько кейсов: active/expected/
    suspicious + idempotency).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.schema import Deployment, Service
from app.scripts.backfill_ownership import (
    BackfillResult,
    plan_ownership,
    plan_stale,
    render_owner_table,
    render_stats,
    run_backfill,
)
from app.services import owner_aliases, ownership_suggester


# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _reset_caches(monkeypatch):
    """Изолируем тесты от global state ownership_suggester / owner_aliases."""
    ownership_suggester.reset_manifest_cache()
    owner_aliases.reset_cache()
    monkeypatch.delenv("OWNERSHIP_MANIFEST_PATH", raising=False)
    monkeypatch.delenv("OWNER_ALIASES_PATH", raising=False)
    yield
    ownership_suggester.reset_manifest_cache()
    owner_aliases.reset_cache()


def _make_svc(
    db,
    name: str,
    namespace: str,
    *,
    team_owner: Optional[str] = None,
    stale_class: Optional[str] = None,
    metadata_json: Optional[dict] = None,
) -> Service:
    svc = Service(
        name=name,
        namespace=namespace,
        team_owner=team_owner,
        stale_class=stale_class,
        metadata_json=metadata_json,
    )
    db.add(svc)
    db.flush()
    return svc


# ── plan_ownership / dry-run ────────────────────────────────────────────


def test_dry_run_picks_squad_prefix_at_default_threshold(db):
    """squad-N prefix даёт confidence=0.4 при weight 0.4 — НЕ проходит
    default threshold 0.5. Dry-run должен показать что они skipped."""
    _make_svc(db, "town-service", "squad-3-shared")  # owner=None
    _make_svc(db, "auth", "squad-7-kingdom1")
    db.commit()

    plans, skipped = plan_ownership(db, threshold=0.5)
    # Только prefix → confidence 0.4 < 0.5 → оба skipped.
    assert plans == []
    assert skipped == 2


def test_dry_run_picks_squad_prefix_at_lower_threshold(db):
    """С threshold 0.3 prefix-only сигналы проходят."""
    _make_svc(db, "town-service", "squad-3-shared")
    _make_svc(db, "auth", "squad-7-kingdom1")
    db.commit()

    plans, skipped = plan_ownership(db, threshold=0.3)
    assert len(plans) == 2
    assert skipped == 0
    by_ns = {p.namespace: p for p in plans}
    assert by_ns["squad-3-shared"].suggested_owner == "squad-3"
    assert by_ns["squad-7-kingdom1"].suggested_owner == "squad-7"
    assert all("prefix" in p.sources for p in plans)


def test_dry_run_does_not_write(db):
    """Plan не должен делать UPDATE — owner остаётся None."""
    svc = _make_svc(db, "town-service", "squad-3-shared")
    db.commit()

    plans, _ = plan_ownership(db, threshold=0.3)
    assert len(plans) == 1
    # Проверяем что БД не тронута.
    db.refresh(svc)
    assert svc.team_owner is None


def test_apply_writes_team_owner_and_owner_source(db):
    svc = _make_svc(db, "town-service", "squad-3-shared")
    db.commit()

    result = run_backfill(db, apply=True, threshold=0.3)
    assert result.actually_updated_owner == 1

    db.refresh(svc)
    assert svc.team_owner == "squad-3"
    assert isinstance(svc.metadata_json, dict)
    assert svc.metadata_json.get("owner_source") == "namespace_prefix"
    assert "owner_confidence" in svc.metadata_json


def test_apply_is_idempotent(db):
    """Второй apply на тех же данных = 0 updates (owner уже стоит)."""
    _make_svc(db, "town-service", "squad-3-shared")
    db.commit()

    r1 = run_backfill(db, apply=True, threshold=0.3)
    assert r1.actually_updated_owner == 1

    # Второй прогон не должен ничего трогать — сервис теперь имеет owner.
    r2 = run_backfill(db, apply=True, threshold=0.3)
    assert r2.actually_updated_owner == 0
    assert r2.total_candidates_owner == 0


def test_threshold_filters_low_confidence(db):
    """confidence prefix-only = 0.4. С threshold 0.5 — skipped."""
    _make_svc(db, "svc", "squad-3-shared")
    db.commit()

    result = run_backfill(db, apply=True, threshold=0.5)
    assert result.actually_updated_owner == 0
    assert result.skipped_low_confidence == 1


def test_manual_override_wins_threshold(db, tmp_path, monkeypatch):
    """Manual manifest даёт confidence=1.0, пишется даже при threshold 0.99."""
    # Manifest: random-ns → @some-team
    manifest = tmp_path / "ownership.yaml"
    manifest.write_text(yaml.safe_dump([
        {"ns_pattern": "weird-ns-*", "owner": "@infra", "reason": "manual-pin"},
    ]))
    monkeypatch.setenv("OWNERSHIP_MANIFEST_PATH", str(manifest))
    ownership_suggester.reset_manifest_cache()

    svc = _make_svc(db, "x", "weird-ns-1")
    db.commit()

    result = run_backfill(db, apply=True, threshold=0.99)
    assert result.actually_updated_owner == 1

    db.refresh(svc)
    assert svc.team_owner == "infra"
    assert svc.metadata_json["owner_source"] == "manual"
    assert svc.metadata_json.get("owner_manual") is True


def test_skips_services_with_existing_owner(db):
    """Сервисы которые уже имеют team_owner не должны попадать в plan."""
    _make_svc(db, "with-owner", "squad-3-shared", team_owner="squad-3")
    _make_svc(db, "without", "squad-7-shared")
    db.commit()

    plans, _ = plan_ownership(db, threshold=0.3)
    assert len(plans) == 1
    assert plans[0].name == "without"


def test_empty_string_owner_treated_as_null(db):
    """team_owner = '' (а не NULL) — тоже считается отсутствующим."""
    _make_svc(db, "svc", "squad-3-shared", team_owner="")
    db.commit()

    plans, _ = plan_ownership(db, threshold=0.3)
    assert len(plans) == 1


def test_limit_caps_plan_size(db):
    for i in range(5):
        _make_svc(db, f"svc-{i}", f"squad-{i}-shared")
    db.commit()

    plans, _ = plan_ownership(db, threshold=0.3, limit=2)
    assert len(plans) == 2


def test_filter_ns_glob(db):
    _make_svc(db, "a", "squad-3-shared")
    _make_svc(db, "b", "prod-kingdom1")
    _make_svc(db, "c", "squad-7-shared")
    db.commit()

    plans, _ = plan_ownership(db, threshold=0.3, filter_ns="squad-*")
    assert {p.namespace for p in plans} == {"squad-3-shared", "squad-7-shared"}


def test_no_candidates_when_all_owned(db):
    _make_svc(db, "a", "squad-3-shared", team_owner="squad-3")
    db.commit()

    result = run_backfill(db, apply=True, threshold=0.3)
    assert result.actually_updated_owner == 0
    assert result.total_candidates_owner == 0
    assert result.kept_existing == 1


def test_metadata_json_merge_preserves_existing_keys(db):
    """При записи owner_source НЕ должны затирать другие metadata-ключи."""
    svc = _make_svc(
        db,
        "svc",
        "squad-3-shared",
        metadata_json={"repo": "github.com/foo/bar", "k8s_ingress": True},
    )
    db.commit()

    run_backfill(db, apply=True, threshold=0.3)

    db.refresh(svc)
    md = svc.metadata_json
    assert md["repo"] == "github.com/foo/bar"
    assert md["k8s_ingress"] is True
    assert md["owner_source"] == "namespace_prefix"


# ── --stale flag ────────────────────────────────────────────────────────


def test_stale_backfill_active_with_recent_deploy(db):
    """Сервис с deploy 5 дней назад → active."""
    svc = _make_svc(db, "town-service", "prod-kingdom1")
    db.add(Deployment(
        service_id=svc.id,
        started_at=datetime.utcnow() - timedelta(days=5),
        status="SUCCESS",
    ))
    db.commit()

    plans = plan_stale(db)
    assert len(plans) == 1
    assert plans[0].new_class == "active"


def test_stale_backfill_expected_for_backup_suffix(db):
    """`*-backup` имя → expected_stale, даже без deploy."""
    _make_svc(db, "postgres-backup", "prod-shared")
    db.commit()

    plans = plan_stale(db)
    assert len(plans) == 1
    assert plans[0].new_class == "expected_stale"


def test_stale_backfill_suspicious_for_regular_no_deploy(db):
    """Обычный app, нет deploys в KG → suspicious_stale."""
    _make_svc(db, "town-service", "prod-kingdom1")
    db.commit()

    plans = plan_stale(db)
    assert len(plans) == 1
    assert plans[0].new_class == "suspicious_stale"


def test_stale_apply_writes_class(db):
    svc = _make_svc(db, "town-service", "prod-kingdom1")
    db.commit()

    result = run_backfill(db, apply=True, do_ownership=False, do_stale=True)
    assert result.actually_updated_stale == 1

    db.refresh(svc)
    assert svc.stale_class == "suspicious_stale"


def test_stale_apply_idempotent(db):
    _make_svc(db, "town-service", "prod-kingdom1")
    db.commit()

    r1 = run_backfill(db, apply=True, do_ownership=False, do_stale=True)
    assert r1.actually_updated_stale == 1
    # Повторный run: stale_class уже выставлен, кандидатов нет.
    r2 = run_backfill(db, apply=True, do_ownership=False, do_stale=True)
    assert r2.actually_updated_stale == 0


def test_combined_ownership_and_stale(db):
    """С обоими флагами оба апдейта применяются за один прогон."""
    svc = _make_svc(db, "town-service", "squad-3-shared")
    db.commit()

    result = run_backfill(
        db, apply=True, threshold=0.3, do_ownership=True, do_stale=True,
    )
    assert result.actually_updated_owner == 1
    assert result.actually_updated_stale == 1

    db.refresh(svc)
    assert svc.team_owner == "squad-3"
    assert svc.stale_class is not None


# ── render helpers ──────────────────────────────────────────────────────


def test_render_owner_table_empty():
    out = render_owner_table([])
    assert "нет кандидатов" in out


def test_render_stats_includes_all_metrics():
    r = BackfillResult(
        kept_existing=1,
        would_update_owner=2,
        skipped_low_confidence=3,
        actually_updated_owner=4,
        total_candidates_owner=2,
    )
    out = render_stats(r)
    assert "kept_existing" in out
    assert "would_update_owner" in out
    assert "skipped_low_confidence" in out
    assert "actually_updated_owner" in out


# ── CLI smoke ───────────────────────────────────────────────────────────


def test_cli_dry_run_smoke(db, monkeypatch, capsys):
    """CLI вход: parse args → запускает run_backfill → печатает markdown.

    Подменяем SessionLocal на нашу in-memory сессию.
    """
    _make_svc(db, "town-service", "squad-3-shared")
    db.commit()

    import app.scripts.backfill_ownership as mod

    def _fake_session_local():
        # Возвращаем session-like wrapper, который игнорирует close().
        class _Wrap:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def close(self):  # noqa: D401 — no-op для shared fixture
                pass

        return _Wrap(db)

    monkeypatch.setattr("app.database.SessionLocal", _fake_session_local)

    rc = mod.main(["--threshold", "0.3"])
    assert rc == 0

    captured = capsys.readouterr()
    assert "Ownership plan" in captured.out
    assert "town-service" in captured.out
    assert "dry-run" in captured.out

    # Не записано — owner всё ещё None.
    svc = db.query(Service).filter_by(name="town-service").one()
    assert svc.team_owner is None
