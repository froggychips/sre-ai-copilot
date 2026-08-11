"""Тесты для shared-infra ownership manifest (`config/ownership.yaml`).

Покрытие:
  - Manifest парсится из репо без ошибок (структурный sanity).
  - ClickHouse → @data в любом `*-shared` ns.
  - NATS → @platform в любом `*-shared` ns.
  - PostgreSQL replicas/backups → @platform.
  - vm-kube-state-metrics → @platform.
  - update-service / seq → @platform.
  - squad-gd-shared apps → @squad-gd через ns catch-all.
  - Per-service `name_pattern` имеет приоритет над generic ns catch-all
    (clickhouse в squad-gd-shared → @data, не @squad-gd).
  - Generic *-shared catch-all (preprod/preupdate/prod) → @platform.

Сценарий: репозиторный `config/ownership.yaml` загружается через
`OWNERSHIP_MANIFEST_PATH`, прогоняем через `suggest_owner_multi_signal`
с per-service именами.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services import owner_aliases, ownership_suggester
from app.services.ownership_suggester import suggest_owner_multi_signal

# Путь к manifest-у в репо (worktree-relative).
_REPO_MANIFEST = Path(__file__).resolve().parents[1] / "config" / "ownership.yaml"


@pytest.fixture(autouse=True)
def _reset_caches(monkeypatch):
    """Подключаем репозиторный manifest и чистим кэши перед каждым тестом."""
    ownership_suggester.reset_manifest_cache()
    owner_aliases.reset_cache()
    monkeypatch.setenv("OWNERSHIP_MANIFEST_PATH", str(_REPO_MANIFEST))
    monkeypatch.delenv("OWNER_ALIASES_PATH", raising=False)
    yield
    ownership_suggester.reset_manifest_cache()
    owner_aliases.reset_cache()


def test_manifest_file_exists_and_parses():
    """Manifest существует, валидный YAML, как минимум 10 правил."""
    assert _REPO_MANIFEST.exists(), f"manifest not found at {_REPO_MANIFEST}"
    rules = ownership_suggester._load_manifest(str(_REPO_MANIFEST))
    assert len(rules) >= 10, f"expected ≥10 rules, got {len(rules)}"
    # Все правила имеют непустой owner.
    for r in rules:
        assert r.owner.strip(), f"empty owner in rule ns={r.ns_pattern}"
        assert r.ns_pattern.strip(), "empty ns_pattern in rule"


def test_clickhouse_in_preprod_shared_to_data():
    sug = suggest_owner_multi_signal(
        "preprod-shared", db=None, name="clickhouse"
    )
    assert sug.owner == "data"
    assert sug.confidence == 1.0
    assert sug.manual is True
    assert sug.sources == ["manual"]


def test_clickhouse_keeper_in_prod_shared_to_data():
    """clickhouse-keeper-headless должен матчиться `clickhouse*` glob → @data."""
    sug = suggest_owner_multi_signal(
        "prod-shared", db=None, name="clickhouse-keeper-headless"
    )
    assert sug.owner == "data"
    assert sug.manual is True


def test_nats_headless_in_prod_shared_to_platform():
    sug = suggest_owner_multi_signal(
        "prod-shared", db=None, name="nats-headless"
    )
    assert sug.owner == "platform"
    assert sug.manual is True


def test_postgresql_replica_in_preprod_shared_to_platform():
    """`chat-db-postgresql-hl` (headless replica) → @platform."""
    sug = suggest_owner_multi_signal(
        "preprod-shared", db=None, name="chat-db-postgresql-hl"
    )
    assert sug.owner == "platform"
    assert sug.manual is True


def test_db_backup_in_squad_gd_shared_to_platform():
    """`*-db-backup` правило выше gd catch-all → @platform, не @squad-gd."""
    sug = suggest_owner_multi_signal(
        "squad-gd-shared", db=None, name="chat-db-backup"
    )
    assert sug.owner == "platform"
    assert sug.manual is True


def test_vm_kube_state_metrics_to_platform():
    sug = suggest_owner_multi_signal(
        "prod-shared", db=None, name="vm-kube-state-metrics"
    )
    assert sug.owner == "platform"
    assert sug.manual is True


def test_update_service_to_platform():
    sug = suggest_owner_multi_signal(
        "squad-gd-shared", db=None, name="update-service"
    )
    assert sug.owner == "platform"
    assert sug.manual is True


def test_squad_gd_app_service_to_squad_gd():
    """`auth-service` в squad-gd-shared не имеет специфичного правила —
    падает в catch-all ns-pattern `squad-gd-shared` → @squad-gd."""
    sug = suggest_owner_multi_signal(
        "squad-gd-shared", db=None, name="auth-service"
    )
    assert sug.owner == "squad-gd"
    assert sug.manual is True


def test_squad_gd_clickhouse_specific_wins_over_catchall():
    """Специфичное правило (clickhouse* в *-shared → @data) выше ns
    catch-all-а squad-gd-shared → @squad-gd. Проверяем что glob order
    в manifest-е работает."""
    sug = suggest_owner_multi_signal(
        "squad-gd-shared", db=None, name="clickhouse"
    )
    assert sug.owner == "data", (
        "clickhouse в squad-gd-shared должен резолвиться в @data через "
        "более специфичное правило выше catch-all-а"
    )


def test_generic_preprod_shared_catchall_to_platform():
    """Несуществующий-в-реальности сервис в preprod-shared попадает
    в ns catch-all → @platform."""
    sug = suggest_owner_multi_signal(
        "preprod-shared", db=None, name="some-random-helper"
    )
    assert sug.owner == "platform"
    assert sug.manual is True


def test_generic_preupdate_shared_catchall_to_platform():
    """`preupdate-shared` покрыт тем же ns catch-all-ом, что prod/preprod.

    Проверяем и обратную сторону фикса `_BARE_SHARED_RE`: manual-правило бьёт
    раньше эвристики, поэтому приведение паттерна к единому списку env ничего
    здесь не меняет — @platform как и раньше.
    """
    sug = suggest_owner_multi_signal(
        "preupdate-shared", db=None, name="some-random-helper"
    )
    assert sug.owner == "platform"
    assert sug.manual is True


def test_shared_catchall_identical_across_real_envs():
    """prod/preprod/preupdate-shared в манифесте ведут себя одинаково."""
    owners = {
        env: suggest_owner_multi_signal(f"{env}-shared", db=None).owner
        for env in ("prod", "preprod", "preupdate")
    }
    assert set(owners.values()) == {"platform"}, owners


def test_preupdate_shared_falls_back_to_multi_squad_without_manifest(monkeypatch):
    """Без манифеста preupdate-shared деградирует в ту же заглушку, что соседи.

    Раньше эвристика отдавала `shared` с полной силой — расхождение было видно
    только при снятом манифесте (backfill с --filter-ns, локальный прогон).
    """
    monkeypatch.delenv("OWNERSHIP_MANIFEST_PATH", raising=False)
    ownership_suggester.reset_manifest_cache()
    for env in ("prod", "preprod", "preupdate"):
        sug = suggest_owner_multi_signal(f"{env}-shared", db=None)
        assert sug.owner == "multi-squad", f"{env}-shared → {sug.owner}"
        assert sug.manual is False


def test_ns_only_call_skips_name_pattern_rules():
    """Если caller не передал name (digest-level вызов), правила с
    name_pattern пропускаются. preprod-shared без name → попадает
    в ns catch-all → @platform."""
    sug = suggest_owner_multi_signal("preprod-shared", db=None)
    # Catch-all без name_pattern всё ещё работает → @platform.
    assert sug.owner == "platform"
    assert sug.manual is True


def test_non_shared_ns_not_matched():
    """ns без `-shared` суффикса не покрывается manifest-ом — должен идти
    через обычные heuristics."""
    sug = suggest_owner_multi_signal(
        "squad-7-kingdom2", db=None, name="auth-service"
    )
    # Manifest не сработал — prefix эвристика дала squad-7.
    assert sug.owner == "squad-7"
    assert sug.manual is False
    assert sug.sources == ["prefix"]
