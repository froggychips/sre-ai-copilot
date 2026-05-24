"""Contract drift check (Gate #22).

Проверяет что:
  1. Все `EDGE_KINDS` со статусом 'active' имеют хотя бы один use в
     `app/knowledge_graph/**` (grep-based, не runtime). Если kind active
     но никем не пишется — drift.
  2. Все строковые kind-литералы в коде (`kind="..."`) попадают в
     `EDGE_KINDS` (active ∪ planned). Unknown — drift.
  3. Все значения `stale_class` из ORM-схемы / classifier-а попадают в
     `contract.STALE_CLASS_VALUES`.
  4. Все источники из `ownership_suggester.OwnerSuggestion.sources`
     резолвятся через `OWNER_SOURCE_ALIASES` в `OWNER_SOURCES`.
  5. `KG_SCHEMA_VERSION` ≥ 2.2 (post-#82/#84/#86 bump).

Это «meta-тест»: он защищает от расхождений между декларацией контракта
и реальной кодовой базой. Падение → либо обновить contract.py, либо
переключить статус kind-а на planned.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Set

import pytest

from app.knowledge_graph.contract import (
    EDGE_KINDS,
    KG_SCHEMA_VERSION,
    OWNER_SOURCE_ALIASES,
    OWNER_SOURCES,
    STALE_CLASS_VALUES,
    active_edge_kinds,
    planned_edge_kinds,
)


# ── Helpers ────────────────────────────────────────────────────────────────


REPO_ROOT = Path(__file__).resolve().parent.parent
KG_DIR = REPO_ROOT / "app" / "knowledge_graph"
APP_DIR = REPO_ROOT / "app"


# Edge kinds которые НЕ пишутся как kg_service_edges rows / kg_volume_edges
# rows (см. EdgeKindSpec.table). Для них grep по `kind="..."` не сработает —
# они живут через FK / metadata column. Исключаем из drift-check.
_NON_EDGE_TABLE_KINDS: Set[str] = {
    kind for kind, spec in EDGE_KINDS.items()
    if spec.get("table") in ("fk_only", "metadata_only")
}


def _read_py_files(directory: Path) -> str:
    """Сконкатенировать содержимое всех .py файлов в директории."""
    parts = []
    for p in directory.rglob("*.py"):
        # Не рекурсируем в __pycache__ etc.
        if "__pycache__" in p.parts:
            continue
        try:
            parts.append(p.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return "\n".join(parts)


# ── 1. active kinds must be used somewhere in kg/ ─────────────────────────


def test_all_active_edge_kinds_used_in_kg_module():
    """Каждый active kind встречается хотя бы один раз в `app/knowledge_graph/`.

    NB: kinds, которые живут не как edge-row (`table='fk_only'` /
    `'metadata_only'`), не должны иметь `kind="..."` литерал в коде —
    они описаны semantically. Их исключаем.
    """
    code = _read_py_files(KG_DIR)
    misses = []
    for kind in active_edge_kinds():
        if kind in _NON_EDGE_TABLE_KINDS:
            continue
        # Ищем литералы вида "kind" или 'kind'.
        pattern = re.compile(rf'["\']{re.escape(kind)}["\']')
        if not pattern.search(code):
            misses.append(kind)
    assert not misses, (
        f"active edge kinds без use в app/knowledge_graph/: {misses}. "
        f"Либо kind не используется (переключить на planned/удалить), "
        f"либо в спеке wrong table-классификация."
    )


# ── 2. all string `kind="..."` literals are known to contract ─────────────


def test_all_string_kind_literals_are_known():
    """Все строки в `kind="..."`-присваиваниях из app/knowledge_graph/**
    должны попадать в EDGE_KINDS.

    Исключаем не-edge kinds (K8sJob.kind, StorageVolume.kind — это
    namespace для других моделей, не edge type). Фильтруем по контексту
    через простой allowlist.
    """
    code = _read_py_files(KG_DIR)
    # Все совпадения `kind="some_word"` (или одинарные кавычки).
    pattern = re.compile(r'\bkind\s*=\s*["\']([a-z_][a-z0-9_]*)["\']')
    all_used = set(pattern.findall(code))

    # Allowlist: kind-литералы которые относятся не к edge-типу, а к
    # node-типу другой модели. Сюда же ходят значения для K8sJob / StorageVolume.
    NON_EDGE_KIND_LITERALS: Set[str] = {
        # K8sJob.kind (`kg_k8s_jobs.kind`): "job" | "cronjob"
        "job", "cronjob",
        # StorageVolume.kind (`kg_storage_volumes.kind`): "pvc" | "pv"
        "pvc", "pv",
    }

    known = set(EDGE_KINDS.keys()) | NON_EDGE_KIND_LITERALS
    unknown = sorted(all_used - known)
    assert not unknown, (
        f"Найдены kind-литералы которые не описаны ни в EDGE_KINDS, "
        f"ни в allowlist (K8sJob.kind / StorageVolume.kind): {unknown}. "
        f"Добавь в contract.EDGE_KINDS либо в allowlist в этом тесте."
    )


def test_no_planned_kinds_currently():
    """После promotion PR #82/#84/#86 — планируемых kinds нет.

    Тест-snapshot: при добавлении нового planned-kind его нужно явно сюда
    внести (или удалить тест). Это защита от ситуации «merge wave, а planned
    забыли переключить на active».
    """
    # Текущий state: ни одного planned.
    planned = planned_edge_kinds()
    assert planned == set(), (
        f"найдены planned kinds: {planned}. Если они в master — промоутни в "
        f"active и обнови этот тест. Если они ещё не в master — обнови тест."
    )


# ── 3. stale_class enum values match contract ─────────────────────────────


def test_stale_class_values_from_classifier_match_contract():
    """`stale_classifier.STALE_CLASS_VALUES` ⊆ `contract.STALE_CLASS_VALUES`."""
    from app.knowledge_graph.stale_classifier import (
        STALE_CLASS_VALUES as CLASSIFIER_VALUES,
    )
    assert set(CLASSIFIER_VALUES) == STALE_CLASS_VALUES, (
        f"drift: classifier={set(CLASSIFIER_VALUES)} vs "
        f"contract={STALE_CLASS_VALUES}"
    )


def test_stale_class_values_used_in_stats_digest():
    """`stats_digest.py` использует canonical константу из contract, не
    хардкоженную строку `"expected_stale"`.
    """
    digest_path = APP_DIR / "services" / "stats_digest.py"
    code = digest_path.read_text(encoding="utf-8")
    # Импорт из contract должен присутствовать.
    assert "STALE_CLASS_EXPECTED_STALE" in code, (
        "stats_digest.py не использует STALE_CLASS_EXPECTED_STALE из contract"
    )


# ── 4. owner sources alignment ───────────────────────────────────────────


def test_owner_source_aliases_map_to_known_canonical():
    """Все алиасы из OWNER_SOURCE_ALIASES.values() — known canonical."""
    for short, canonical in OWNER_SOURCE_ALIASES.items():
        assert canonical in OWNER_SOURCES, (
            f"alias {short!r} → {canonical!r}, но {canonical!r} нет в OWNER_SOURCES"
        )


def test_ownership_suggester_signal_names_known():
    """Все источники, которые `ownership_suggester` пишет в
    `OwnerSuggestion.sources`, должны быть либо алиасами либо canonical.
    """
    # Hardcoded list — это «декларация»: при добавлении нового сигнала
    # сюда тоже надо добавить. Защита от drift «новый сигнал, забыл
    # отразить в OWNER_SOURCES».
    expected_signals = {"prefix", "deploy_history", "labels", "manual"}
    for sig in expected_signals:
        canonical = OWNER_SOURCE_ALIASES.get(sig)
        assert canonical is not None, (
            f"signal {sig!r} не имеет alias в OWNER_SOURCE_ALIASES"
        )
        assert canonical in OWNER_SOURCES, (
            f"alias {sig!r} → {canonical!r}, но в OWNER_SOURCES такого нет"
        )


# ── 5. schema version bump ───────────────────────────────────────────────


def test_kg_schema_version_at_least_2_2():
    """После PR #82/#84/#86 минимальная версия — 2.2."""
    parts = KG_SCHEMA_VERSION.split(".")
    major, minor = int(parts[0]), int(parts[1])
    assert (major, minor) >= (2, 2), (
        f"KG_SCHEMA_VERSION={KG_SCHEMA_VERSION!r} — ожидается ≥ 2.2 "
        f"после PR #82/#84/#86 (jobs/storage/stale_class)."
    )


# ── 6. docs alignment ────────────────────────────────────────────────────


def test_docs_mention_current_schema_version():
    """`docs/KG_SCHEMA_CONTRACT.md` упоминает текущую версию."""
    doc = (REPO_ROOT / "docs" / "KG_SCHEMA_CONTRACT.md").read_text(encoding="utf-8")
    assert KG_SCHEMA_VERSION in doc, (
        f"docs/KG_SCHEMA_CONTRACT.md не упоминает {KG_SCHEMA_VERSION!r}. "
        f"Обнови документ при bump-е версии."
    )


def test_docs_list_all_active_edge_kinds():
    """Все active edge kinds упомянуты в docs/KG_SCHEMA_CONTRACT.md."""
    doc = (REPO_ROOT / "docs" / "KG_SCHEMA_CONTRACT.md").read_text(encoding="utf-8")
    misses = [k for k in active_edge_kinds() if k not in doc]
    assert not misses, (
        f"active kinds отсутствуют в docs/KG_SCHEMA_CONTRACT.md: {misses}"
    )


# ── 7. Migrations — values consistency ───────────────────────────────────


def test_stale_class_migration_uses_string_column():
    """Миграция 20260524_0200 добавляет колонку `stale_class` как String,
    а не PG enum — это намеренно для sqlite-compat тестов.
    """
    mig = REPO_ROOT / "alembic" / "versions" / "20260524_0200_add_kg_services_stale_class.py"
    assert mig.exists(), "миграция stale_class отсутствует"
    code = mig.read_text(encoding="utf-8")
    assert "stale_class" in code
    # Не PG enum (нет sa.Enum или ENUM())
    assert "sa.Enum" not in code and "ENUM(" not in code, (
        "stale_class должен быть String, не PG enum — потеряем sqlite-compat"
    )
