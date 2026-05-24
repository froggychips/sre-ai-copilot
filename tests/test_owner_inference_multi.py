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
    """prefix `squad-7-shared` → squad-7 с confidence = 0.5 (weight A bump)."""
    sug = suggest_owner_multi_signal("squad-7-shared", db=None)
    assert sug.owner == "squad-7"
    assert sug.sources == ["prefix"]
    assert pytest.approx(sug.confidence, abs=1e-6) == 0.5
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
    # strength = 8/10 = 0.8, weight 0.3 → 0.24
    assert pytest.approx(sug.confidence, abs=1e-6) == 0.3 * 0.8


def test_signal_b_unknown_username_no_signal():
    """Неизвестный username не контрибутит — сигнал B молчит.

    Раньше возвращали `?-someuser` с confidence weight*strength — это
    было ложно-высокое доверие. Теперь only known aliases вносят strength.
    """
    db = _mock_db_with_responses(deploys=[("someuser", 5)], labels=[])
    sug = suggest_owner_multi_signal("weird-ns", db)
    assert sug.owner is None
    assert "deploy_history" not in sug.sources


def test_signal_b_known_user_amid_unknown():
    """Top-1 unknown, top-2 known → берём top-2, strength = top2 / total."""
    db = _mock_db_with_responses(
        deploys=[("unknown-bot", 7), ("kemyashev", 3)],
        labels=[],
    )
    sug = suggest_owner_multi_signal("weird-ns", db)
    assert sug.owner == "squad-1"
    # strength = 3/10 = 0.3, weight 0.3 → 0.09
    assert pytest.approx(sug.confidence, abs=1e-6) == 0.3 * 0.3


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


# ── Signal C: labels extract — расширенные кейсы (2026-05-24) ────────────


def test_signal_c_labels_k8s_labels_subkey():
    """metadata.k8s_labels.team — альтернативное nested место."""
    db = _mock_db_with_responses(
        deploys=[],
        labels=[
            ({"k8s_labels": {"team": "squad-12"}},),
            ({"k8s_labels": {"team": "squad-12"}},),
        ],
    )
    sug = suggest_owner_multi_signal("weird-ns", db)
    assert sug.owner == "squad-12"
    assert "labels" in sug.sources


def test_signal_c_labels_managed_by_fallback():
    """managed-by label как fallback для платформенных компонентов."""
    db = _mock_db_with_responses(
        deploys=[],
        labels=[({"labels": {"app.kubernetes.io/managed-by": "Helm"}},)],
    )
    sug = suggest_owner_multi_signal("weird-ns", db)
    # managed-by="Helm" — это owner-токен (нормализован).
    assert sug.owner == "Helm"


def test_signal_c_labels_normalize_squad_shorthand():
    """`squad7` (без дефиса) нормализуется в `squad-7`."""
    db = _mock_db_with_responses(
        deploys=[],
        labels=[({"labels": {"team": "squad7"}},)],
    )
    sug = suggest_owner_multi_signal("weird-ns", db)
    assert sug.owner == "squad-7"


def test_signal_c_labels_strip_at_prefix():
    """`@squad-3` в label-value нормализуется в `squad-3` (без `@`).

    Это важно — caller добавляет `@` сам; иначе на выходе будет `@@squad-3`.
    """
    db = _mock_db_with_responses(
        deploys=[],
        labels=[({"labels": {"owner": "@squad-3"}},)],
    )
    sug = suggest_owner_multi_signal("weird-ns", db)
    assert sug.owner == "squad-3"


def test_signal_c_labels_priority_nested_over_flat():
    """`labels.team` имеет приоритет над flat `team` (в одной metadata).

    Раньше flat ключи проверялись после labels — оставляем то же поведение,
    но тестируем явно после расширения sub-keys.
    """
    db = _mock_db_with_responses(
        deploys=[],
        labels=[
            ({"labels": {"team": "from-nested"}, "team": "from-flat"},),
        ],
    )
    sug = suggest_owner_multi_signal("weird-ns", db)
    assert sug.owner == "from-nested"


# ── Multi-signal fusion ──────────────────────────────────────────────────


def test_fusion_three_signals_agree():
    """A + C голосуют за squad-7; B unknown → не контрибутит. Confidence=0.7."""
    db = _mock_db_with_responses(
        deploys=[("kemyashev_unmapped", 10)],  # unknown → B молчит
        labels=[({"labels": {"team": "squad-7"}},)],
    )
    # prefix squad-7-x → squad-7  (weight 0.5)
    # labels → squad-7             (weight 0.2 * 1.0)
    # deploy → unknown            (signal silent)
    sug = suggest_owner_multi_signal("squad-7-shared", db)
    assert sug.owner == "squad-7"
    # A=0.5, C=0.2 → 0.7
    assert pytest.approx(sug.confidence, abs=1e-6) == 0.7
    assert "prefix" in sug.sources
    assert "labels" in sug.sources
    # B silent — не в sources.
    assert "deploy_history" not in sug.sources


def test_fusion_all_three_signals_same_owner():
    """Все 3 указывают на squad-1 → confidence ≈ 1.0 (max)."""
    db = _mock_db_with_responses(
        deploys=[("kemyashev", 10)],  # → squad-1, strength 1.0
        labels=[({"labels": {"team": "squad-1"}},)],
    )
    sug = suggest_owner_multi_signal("squad-1-shared", db)
    assert sug.owner == "squad-1"
    # A=0.5 + B=0.3*1.0=0.3 + C=0.2*1.0=0.2 = 1.0
    assert pytest.approx(sug.confidence, abs=1e-6) == 1.0


def test_fusion_signals_disagree_highest_wins():
    """A=squad-7 (0.5), B=apleshkov→squad-2 (0.3*1.0=0.3) → A wins by score."""
    db = _mock_db_with_responses(deploys=[("apleshkov", 10)], labels=[])
    sug = suggest_owner_multi_signal("squad-7-shared", db)
    # Prefix (0.5) > deploy (0.3) → squad-7 побеждает однозначно.
    assert sug.owner == "squad-7"
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
    # B=0.3 + C=0.2 = 0.5
    assert pytest.approx(sug.confidence, abs=1e-6) == 0.5


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

    # partial — только prefix → 0.5 (bumped из 0.4)
    s_partial = suggest_owner_multi_signal("monitoring", db=None)
    assert pytest.approx(s_partial.confidence, abs=1e-6) == 0.5

    # strong — два согласованных сигнала (prefix + deploy)
    db_strong = _mock_db_with_responses(
        deploys=[("kemyashev", 10)],
        labels=[],
    )
    s_strong = suggest_owner_multi_signal("squad-1-shared", db_strong)
    # 0.5 + 0.3*1.0 = 0.8
    assert pytest.approx(s_strong.confidence, abs=1e-6) == 0.8

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


def test_owner_aliases_bundled_yaml_resolves():
    """Bundled `owner_aliases.yaml` рядом с модулем подгружается без ENV.

    Проверяем что расширенные aliases (squad-gd / backend / platform reviewers)
    резолвятся из YAML по умолчанию.
    """
    # Расширения из bundled YAML (см. app/services/owner_aliases.yaml).
    # `igoncharov` → @squad-gd (раньше был fallback @?-igoncharov).
    assert owner_aliases.resolve_username("igoncharov") == "@squad-gd"


def test_owner_aliases_bundled_yaml_platform_reviewers():
    """Несколько platform-reviewers резолвятся в @platform."""
    # Из bundled YAML.
    assert owner_aliases.resolve_username("zbushuev") == "@platform"
    assert owner_aliases.resolve_username("sgrozov") == "@platform"
    assert owner_aliases.resolve_username("pryzhikov") == "@platform"


def test_owner_aliases_is_known_helper():
    """`is_known_username` различает aliased / fallback users.

    Используется сигналом B (deploy_history) чтобы не выдавать
    `?-username` с положительным weight.
    """
    assert owner_aliases.is_known_username("kemyashev") is True
    assert owner_aliases.is_known_username("igoncharov") is True  # bundled
    assert owner_aliases.is_known_username("totally-random-bot") is False
    assert owner_aliases.is_known_username("") is False
    # Case-insensitive.
    assert owner_aliases.is_known_username("KEMYASHEV") is True


def test_owner_aliases_bundled_size_at_least_15():
    """Sanity-check на размер bundled YAML.

    Если кто-то случайно удалит aliases — рантайм-фолбэк на дефолты резко
    урежет покрытие deploy_history сигнала. 15 — нижняя граница, актуальный
    список ожидаемо больше.
    """
    all_aliases = owner_aliases.get_aliases()
    assert len(all_aliases) >= 15


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


def test_unowned_section_medium_confidence_prefix_only():
    """Prefix-only для bare 'monitoring' → confidence=0.5 → medium (без `?`, без bold).

    После bump _W_PREFIX 0.4 → 0.5 prefix-only достигает default backfill
    threshold. UI убирает `?`-суффикс, но не bolds (< 0.8).
    """
    unowned: defaultdict = defaultdict(int)
    unowned["monitoring"] = 44
    text = stats_digest.unowned_namespaces_section(unowned, db=None)
    assert "@platform" in text
    # Не bold (confidence < 0.8).
    assert "**`@platform`**" not in text
    # И не `?`-suffixed (confidence == 0.5, не < 0.5).
    assert "@platform` ?" not in text


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
    """squad-1-shared + deploy от kemyashev → confidence 0.74 → не bold."""
    unowned: defaultdict = defaultdict(int)
    unowned["squad-1-shared"] = 10

    db = _mock_db_with_responses(
        deploys=[("kemyashev", 8), ("other", 2)],  # squad-1, strength 0.8
        labels=[],
    )
    text = stats_digest.unowned_namespaces_section(unowned, db=db)
    # A=0.5 + B=0.3*0.8=0.24 = 0.74 → не bold (< 0.8), но > 0.5 → нет `?`.
    assert "@squad-1" in text
    assert "**`@squad-1`**" not in text  # not bold (< 0.8)
