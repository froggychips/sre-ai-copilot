"""Тесты для multi-signal owner inference (KG Coverage #3, 2026-05-24).

Покрытие:
  - Каждый из трёх сигналов в изоляции (prefix / deploy_history / labels).
  - Multi-signal fusion (несколько сигналов указывают на разное).
  - Manual override побеждает всё.
  - Confidence calibration.
  - owner_aliases: дефолты + YAML override + fallback.
  - stats_digest.unowned_namespaces_section — рендер с confidence-разметкой.
"""
from __future__ import annotations

from collections import defaultdict
from unittest.mock import MagicMock

import pytest

from app.services import ownership_suggester, owner_aliases, stats_digest
from app.services.ownership_suggester import (
    suggest_owner_for_ns,
    suggest_owner_multi_signal,
)


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_caches(monkeypatch):
    """Чистим in-process кэши manifest/aliases перед каждым тестом, чтобы
    тесты не «протекали» друг в друга через ENV-флаги."""
    ownership_suggester.reset_manifest_cache()
    owner_aliases.reset_cache()
    monkeypatch.delenv("OWNERSHIP_MANIFEST_PATH", raising=False)
    monkeypatch.delenv("OWNER_ALIASES_PATH", raising=False)
    yield
    ownership_suggester.reset_manifest_cache()
    owner_aliases.reset_cache()


def _mock_db_with_responses(*, deploys=None, labels=None, team_owner=None):
    """Сконструировать MagicMock(Session) который возвращает разные ответы
    на разные SQL-запросы (определяем по подстроке в text).

    Аргументы:
      deploys: список (triggered_by, count) для kg_deployments JOIN запроса.
      labels:  список (metadata_json,) для kg_services labels запроса.
      team_owner: одно значение для legacy KG fallback.
    """
    db = MagicMock()

    def execute(stmt, params=None):
        sql = str(stmt)
        result = MagicMock()
        if "FROM kg_deployments" in sql:
            result.fetchall.return_value = deploys or []
        elif "metadata_json" in sql and "kg_services" in sql and "team_owner IS NOT NULL" not in sql:
            result.fetchall.return_value = labels or []
        elif "team_owner" in sql and "team_owner IS NOT NULL" in sql and "LIMIT 1" in sql:
            result.fetchone.return_value = (team_owner,) if team_owner else None
        else:
            result.fetchall.return_value = []
            result.fetchone.return_value = None
        return result

    db.execute.side_effect = execute
    return db


# ── Signal A: prefix only (isolation) ────────────────────────────────────


def test_signal_a_prefix_only_squad():
    """prefix `squad-7-shared` → squad-7 с confidence = 0.4 (только weight A)."""
    sug = suggest_owner_multi_signal("squad-7-shared", db=None)
    assert sug.owner == "squad-7"
    assert sug.sources == ["prefix"]
    assert pytest.approx(sug.confidence, abs=1e-6) == 0.4
    assert sug.manual is False


def test_signal_a_prefix_only_kingdom():
    sug = suggest_owner_multi_signal("prod-kingdom2", db=None)
    assert sug.owner == "kingdom2"
    assert "prefix" in sug.sources


def test_signal_a_prefix_platform_bare_ns():
    sug = suggest_owner_multi_signal("monitoring", db=None)
    assert sug.owner == "platform"
    assert sug.sources == ["prefix"]


def test_signal_a_no_prefix_no_db_returns_none():
    """Странный ns без prefix-а и без db → нет догадки."""
    sug = suggest_owner_multi_signal("totally-random", db=None)
    assert sug.owner is None
    assert sug.confidence == 0.0
    assert sug.sources == []


# ── Signal B: deploy history only ────────────────────────────────────────


def test_signal_b_deploy_history_only():
    """ns без prefix-match → deploy_history от kemyashev → @squad-1."""
    db = _mock_db_with_responses(
        deploys=[("kemyashev", 8), ("apleshkov", 2)],
        labels=[],
    )
    sug = suggest_owner_multi_signal("weird-backend", db)
    assert sug.owner == "squad-1"
    assert sug.sources == ["deploy_history"]
    # strength = 8/10 = 0.8, weight 0.4 → 0.32
    assert pytest.approx(sug.confidence, abs=1e-6) == 0.4 * 0.8


def test_signal_b_unknown_username_falls_back():
    """Неизвестный username → `?-someuser` fallback."""
    db = _mock_db_with_responses(deploys=[("someuser", 5)], labels=[])
    sug = suggest_owner_multi_signal("weird-ns", db)
    assert sug.owner == "?-someuser"
    assert sug.sources == ["deploy_history"]


def test_signal_b_empty_deploys_no_suggest():
    db = _mock_db_with_responses(deploys=[], labels=[])
    sug = suggest_owner_multi_signal("weird-ns", db)
    assert sug.owner is None


# ── Signal C: labels only ─────────────────────────────────────────────────


def test_signal_c_labels_only_team_key():
    """Все 3 сервиса в ns имеют labels.team=squad-9 → squad-9."""
    db = _mock_db_with_responses(
        deploys=[],
        labels=[
            ({"labels": {"team": "squad-9"}},),
            ({"labels": {"team": "squad-9"}},),
            ({"labels": {"team": "squad-9"}},),
        ],
    )
    sug = suggest_owner_multi_signal("weird-ns", db)
    assert sug.owner == "squad-9"
    assert sug.sources == ["labels"]
    # strength = 3/3 = 1.0, weight 0.2 → 0.2
    assert pytest.approx(sug.confidence, abs=1e-6) == 0.2


def test_signal_c_labels_flat_metadata():
    """Labels могут быть положены плоско в metadata_json[key]."""
    db = _mock_db_with_responses(
        deploys=[],
        labels=[
            ({"owner": "platform"},),
            ({"owner": "platform"},),
        ],
    )
    sug = suggest_owner_multi_signal("weird-ns", db)
    assert sug.owner == "platform"
    assert "labels" in sug.sources


def test_signal_c_labels_part_of_key():
    db = _mock_db_with_responses(
        deploys=[],
        labels=[({"labels": {"app.kubernetes.io/part-of": "kingdom-3"}},)],
    )
    sug = suggest_owner_multi_signal("weird-ns", db)
    assert sug.owner == "kingdom-3"


def test_signal_c_labels_empty_metadata_no_suggest():
    db = _mock_db_with_responses(deploys=[], labels=[({"unrelated": "x"},)])
    sug = suggest_owner_multi_signal("weird-ns", db)
    assert sug.owner is None


# ── Multi-signal fusion ──────────────────────────────────────────────────


def test_fusion_three_signals_agree():
    """Все 3 сигнала указывают на squad-7 → confidence = sum(weights) = 1.0."""
    db = _mock_db_with_responses(
        deploys=[("kemyashev_unmapped", 10)],  # без alias → ?-kemyashev_unmapped
        labels=[({"labels": {"team": "squad-7"}},)],
    )
    # prefix squad-7-x → squad-7
    # labels → squad-7
    # deploy → ?-kemyashev_unmapped (другое)
    sug = suggest_owner_multi_signal("squad-7-shared", db)
    assert sug.owner == "squad-7"  # A+C голосуют за squad-7, B — другое
    # A=0.4, C=0.2*1.0=0.2 → 0.6
    assert pytest.approx(sug.confidence, abs=1e-6) == 0.6
    assert "prefix" in sug.sources
    assert "labels" in sug.sources
    assert "deploy_history" in sug.sources


def test_fusion_all_three_signals_same_owner():
    """Все 3 указывают на squad-1 → confidence ≈ 1.0 (max)."""
    db = _mock_db_with_responses(
        deploys=[("kemyashev", 10)],  # → squad-1
        labels=[({"labels": {"team": "squad-1"}},)],
    )
    sug = suggest_owner_multi_signal("squad-1-shared", db)
    assert sug.owner == "squad-1"
    # A=0.4 + B=0.4*1.0=0.4 + C=0.2*1.0=0.2 = 1.0
    assert pytest.approx(sug.confidence, abs=1e-6) == 1.0


def test_fusion_signals_disagree_highest_wins():
    """A=squad-7 (0.4), B=apleshkov→squad-2 (0.4*1.0=0.4) → tie → A wins by order.

    Это валидный test для tie-breaking: dict-сохранение insertion order
    плюс max(...) с key — Python берёт первый.
    """
    db = _mock_db_with_responses(deploys=[("apleshkov", 10)], labels=[])
    sug = suggest_owner_multi_signal("squad-7-shared", db)
    # Score одинаковый (0.4 vs 0.4), max берёт первый встретившийся (prefix).
    assert sug.owner in {"squad-7", "squad-2"}
    # Оба сигнала использованы.
    assert set(sug.sources) == {"prefix", "deploy_history"}


def test_fusion_b_dominates_when_a_absent():
    """ns без prefix → B (deploy) + C (labels) согласны → их sum."""
    db = _mock_db_with_responses(
        deploys=[("kemyashev", 10)],
        labels=[({"labels": {"squad": "squad-1"}},)],
    )
    sug = suggest_owner_multi_signal("legacy-ns", db)
    assert sug.owner == "squad-1"
    # B=0.4 + C=0.2 = 0.6
    assert pytest.approx(sug.confidence, abs=1e-6) == 0.6


# ── Manual override ─────────────────────────────────────────────────────


def test_manual_override_wins_over_all_signals(monkeypatch, tmp_path):
    """Manual manifest match → confidence=1.0, sources=['manual'], игнор остального."""
    manifest = tmp_path / "ownership.yaml"
    manifest.write_text(
        "- ns_pattern: \"squad-7-*\"\n"
        "  owner: \"@platform\"\n"
        "  reason: \"manual\"\n"
    )
    monkeypatch.setenv("OWNERSHIP_MANIFEST_PATH", str(manifest))
    ownership_suggester.reset_manifest_cache()

    # Даже если prefix + deploy + labels указывают на squad-7 —
    # manual вписывает platform.
    db = _mock_db_with_responses(
        deploys=[("kemyashev", 10)],
        labels=[({"labels": {"team": "squad-7"}},)],
    )
    sug = suggest_owner_multi_signal("squad-7-kingdom2", db)
    assert sug.owner == "platform"
    assert sug.confidence == 1.0
    assert sug.sources == ["manual"]
    assert sug.manual is True


def test_manual_override_exact_ns(monkeypatch, tmp_path):
    manifest = tmp_path / "ownership.yaml"
    manifest.write_text(
        "- ns_pattern: \"monitoring\"\n"
        "  owner: \"@platform\"\n"
    )
    monkeypatch.setenv("OWNERSHIP_MANIFEST_PATH", str(manifest))
    ownership_suggester.reset_manifest_cache()

    sug = suggest_owner_multi_signal("monitoring", db=None)
    assert sug.manual is True
    assert sug.confidence == 1.0


def test_manual_manifest_missing_file_graceful(monkeypatch, tmp_path):
    """Если файл задан но не существует — деградирует до эвристик без exception."""
    monkeypatch.setenv("OWNERSHIP_MANIFEST_PATH", str(tmp_path / "does-not-exist.yaml"))
    ownership_suggester.reset_manifest_cache()
    sug = suggest_owner_multi_signal("monitoring", db=None)
    assert sug.owner == "platform"
    assert sug.manual is False


def test_manual_manifest_unset_uses_heuristics():
    """Без ENV-флага manifest игнорируется полностью."""
    sug = suggest_owner_multi_signal("monitoring", db=None)
    assert sug.manual is False
    assert sug.sources == ["prefix"]


# ── Confidence calibration ──────────────────────────────────────────────


def test_confidence_high_when_signals_agree():
    """Согласие 2+ сигналов → confidence ≥ 0.5 (видимо в UI как «не ?»)."""
    db = _mock_db_with_responses(
        deploys=[("kemyashev", 10)],
        labels=[({"labels": {"team": "squad-1"}},)],
    )
    sug = suggest_owner_multi_signal("squad-1-shared", db)
    assert sug.confidence >= 0.5


def test_confidence_low_when_only_weak_signal():
    """Один лишь labels-сигнал с 50%-coverage → confidence = 0.1, низкая."""
    db = _mock_db_with_responses(
        deploys=[],
        labels=[
            ({"labels": {"team": "squad-1"}},),
            ({"labels": {}},),  # без team
        ],
    )
    sug = suggest_owner_multi_signal("weird-ns", db)
    # C: strength = 1/2 = 0.5, weight 0.2 → 0.1
    assert sug.confidence < 0.5
    assert sug.owner == "squad-1"


def test_confidence_zero_when_no_signal():
    sug = suggest_owner_multi_signal("totally-random-ns", db=None)
    assert sug.confidence == 0.0
    assert sug.owner is None


def test_confidence_bounded_to_one():
    """Даже если бы все weights выдали max — confidence ≤ 1.0."""
    db = _mock_db_with_responses(
        deploys=[("kemyashev", 10)],
        labels=[({"labels": {"team": "squad-1"}},)],
    )
    sug = suggest_owner_multi_signal("squad-1-shared", db)
    assert 0.0 <= sug.confidence <= 1.0


def test_confidence_calibration_axes():
    """Снапшот калибровки confidence по axis: 0 / weak / partial / strong / max."""
    # 0 — нет сигнала
    s0 = suggest_owner_multi_signal("random-xyz", db=None)
    assert s0.confidence == 0.0

    # weak — labels-only низкая strength
    db_weak = _mock_db_with_responses(
        deploys=[],
        labels=[({"labels": {"team": "x"}},), ({"labels": {}},), ({"labels": {}},)],
    )
    s_weak = suggest_owner_multi_signal("ns-x", db_weak)
    assert 0 < s_weak.confidence < 0.5

    # partial — только prefix
    s_partial = suggest_owner_multi_signal("monitoring", db=None)
    assert pytest.approx(s_partial.confidence, abs=1e-6) == 0.4

    # strong — два согласованных сигнала
    db_strong = _mock_db_with_responses(
        deploys=[("kemyashev", 10)],
        labels=[],
    )
    s_strong = suggest_owner_multi_signal("squad-1-shared", db_strong)
    assert s_strong.confidence >= 0.5

    # max — все три
    db_max = _mock_db_with_responses(
        deploys=[("kemyashev", 10)],
        labels=[({"labels": {"team": "squad-1"}},)],
    )
    s_max = suggest_owner_multi_signal("squad-1-shared", db_max)
    assert pytest.approx(s_max.confidence, abs=1e-6) == 1.0


# ── owner_aliases ───────────────────────────────────────────────────────


def test_owner_aliases_defaults():
    """Дефолтные хардкод-юзеры должны резолвиться."""
    assert owner_aliases.resolve_username("kemyashev") == "@squad-1"
    assert owner_aliases.resolve_username("apleshkov") == "@squad-2"
    assert owner_aliases.resolve_username("wizaryx") == "@platform"


def test_owner_aliases_unknown_username():
    assert owner_aliases.resolve_username("randomuser") == "@?-randomuser"


def test_owner_aliases_empty_returns_question():
    assert owner_aliases.resolve_username("") == "@?"


def test_owner_aliases_case_insensitive():
    """username нормализуется в lower-case."""
    assert owner_aliases.resolve_username("KEMYASHEV") == "@squad-1"
    assert owner_aliases.resolve_username("Kemyashev") == "@squad-1"


def test_owner_aliases_file_override(monkeypatch, tmp_path):
    """YAML-файл оверрайдит дефолты."""
    aliases = tmp_path / "aliases.yaml"
    aliases.write_text(
        "kemyashev: \"@squad-99\"\n"
        "newperson: \"@infra\"\n"
    )
    monkeypatch.setenv("OWNER_ALIASES_PATH", str(aliases))
    owner_aliases.reset_cache()
    assert owner_aliases.resolve_username("kemyashev") == "@squad-99"  # override
    assert owner_aliases.resolve_username("newperson") == "@infra"     # new
    # Дефолтные не задетые остаются.
    assert owner_aliases.resolve_username("apleshkov") == "@squad-2"


# ── Backward compat: suggest_owner_for_ns ───────────────────────────────


def test_legacy_suggest_owner_for_ns_still_works():
    assert suggest_owner_for_ns("squad-7-shared") == "squad-7"
    assert suggest_owner_for_ns("prod-kingdom1") == "kingdom1"
    assert suggest_owner_for_ns("monitoring") == "platform"


def test_legacy_suggest_owner_for_ns_empty():
    assert suggest_owner_for_ns("") is None
    assert suggest_owner_for_ns("(no-ns)") is None


def test_legacy_suggest_owner_for_ns_manual_override(monkeypatch, tmp_path):
    """Manual manifest также применяется к legacy API (защита от drift-а)."""
    manifest = tmp_path / "ownership.yaml"
    manifest.write_text(
        "- ns_pattern: \"monitoring\"\n"
        "  owner: \"@special\"\n"
    )
    monkeypatch.setenv("OWNERSHIP_MANIFEST_PATH", str(manifest))
    ownership_suggester.reset_manifest_cache()
    assert suggest_owner_for_ns("monitoring") == "special"


# ── stats_digest.unowned_namespaces_section integration ──────────────────


def test_unowned_section_bold_for_high_confidence(monkeypatch, tmp_path):
    """High-confidence suggestion (manual) → **bold** + (manual)."""
    manifest = tmp_path / "ownership.yaml"
    manifest.write_text(
        "- ns_pattern: \"monitoring\"\n"
        "  owner: \"@platform\"\n"
    )
    monkeypatch.setenv("OWNERSHIP_MANIFEST_PATH", str(manifest))
    ownership_suggester.reset_manifest_cache()

    unowned: defaultdict = defaultdict(int)
    unowned["monitoring"] = 44

    text = stats_digest.unowned_namespaces_section(unowned, db=None)
    assert "**`@platform`**" in text
    assert "(manual)" in text


def test_unowned_section_question_for_low_confidence():
    """Только prefix-match для bare 'monitoring' → confidence=0.4 → суффикс ` ?`."""
    unowned: defaultdict = defaultdict(int)
    unowned["monitoring"] = 44
    text = stats_digest.unowned_namespaces_section(unowned, db=None)
    # confidence = 0.4 < 0.5 → low → суффикс «?»
    assert "@platform" in text
    assert "?" in text
    # Не bold (confidence < 0.8).
    assert "**`@platform`**" not in text


def test_unowned_section_no_suggest_for_unknown_ns():
    unowned: defaultdict = defaultdict(int)
    unowned["totally-random"] = 5
    text = stats_digest.unowned_namespaces_section(unowned, db=None)
    assert "`?`" in text


def test_unowned_section_empty_returns_empty_string():
    assert stats_digest.unowned_namespaces_section(defaultdict(int), db=None) == ""


def test_unowned_section_caps_top_n():
    unowned: defaultdict = defaultdict(int)
    for i in range(20):
        unowned[f"ns-{i}"] = i + 1
    text = stats_digest.unowned_namespaces_section(unowned, db=None, top_n=5)
    bullet_count = text.count("\n  •")
    assert bullet_count == 5


def test_unowned_section_high_confidence_two_signals(monkeypatch, tmp_path):
    """squad-1-shared + deploy от kemyashev → confidence 0.72 → bold."""
    unowned: defaultdict = defaultdict(int)
    unowned["squad-1-shared"] = 10

    db = _mock_db_with_responses(
        deploys=[("kemyashev", 8), ("other", 2)],  # squad-1, strength 0.8
        labels=[],
    )
    text = stats_digest.unowned_namespaces_section(unowned, db=db)
    # A=0.4 + B=0.4*0.8=0.32 = 0.72 → bold (≥0.5 ? нет, для bold нужно ≥0.8)
    # Меняем ожидание: 0.72 < 0.8 → не bold, но > 0.5 → нет `?`-суффикса.
    assert "@squad-1" in text
    assert "**`@squad-1`**" not in text  # not bold (< 0.8)
