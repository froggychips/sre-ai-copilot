"""Имена constraint'ов в ON CONFLICT должны существовать в схеме.

Инцидент 08.08.2026. Миграция 20260807_0200 переименовала уникальный ключ
`kg_services` из `uq_kg_service_ns_name` в `uq_kg_service_ns_name_kind`
(ключ стал трёхколоночным). В `populator._upsert_service_pg` имя обновили,
а в `kg_sync._upsert_service_pg` — ВТОРОЙ копии той же логики — нет.

Последствие: `kg_topology_sync` падал на КАЖДОМ namespace с
`UndefinedObject: constraint ... does not exist` — 79 ошибок за тик,
`services=0`. Граф по сервисам не обновлялся сутки, и заметили это только
при ручном прогоне синка: в статистике `edges` шли ненулевые, а `services=0`
читалось как «нечего обновлять».

Юнит-тесты этого не ловили: они гоняются на SQLite, где ON CONFLICT по имени
constraint не проверяется. Поэтому тест структурный — сверяет имена в коде с
именами в модели.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from app.knowledge_graph.schema import Service


def _declared_constraint_names() -> set[str]:
    """Имена UNIQUE-констрейнтов, объявленные в модели."""
    names = set()
    for arg in getattr(Service, "__table_args__", ()) or ():
        name = getattr(arg, "name", None)
        if name:
            names.add(name)
    for c in Service.__table__.constraints:
        if getattr(c, "name", None):
            names.add(c.name)
    for idx in Service.__table__.indexes:
        if idx.unique and idx.name:
            names.add(idx.name)
    return names


_APP = pathlib.Path("app")


def _constraint_refs() -> list[tuple[str, int, str]]:
    """Все `constraint="..."` в вызовах on_conflict_do_update."""
    refs = []
    for path in sorted(_APP.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        if "on_conflict_do_update" not in src:
            continue
        for m in re.finditer(r'constraint\s*=\s*"([^"]+)"', src):
            line = src[:m.start()].count("\n") + 1
            refs.append((path.as_posix(), line, m.group(1)))
    return refs


def test_all_referenced_constraints_exist_in_model():
    """Ни один ON CONFLICT не ссылается на несуществующий constraint."""
    declared = _declared_constraint_names()
    unknown = [
        f"{path}:{line} → {name!r}"
        for path, line, name in _constraint_refs()
        # kg_services — единственная таблица с именованным ключом в этих
        # вызовах; остальные (если появятся) проверяются по своей модели.
        if name.startswith("uq_kg_service") and name not in declared
    ]
    assert not unknown, (
        "ON CONFLICT ссылается на констрейнт, которого нет в модели "
        f"(есть: {sorted(declared)}):\n  " + "\n  ".join(unknown)
    )


def test_kg_services_key_includes_node_kind():
    """Ключ kg_services трёхколоночный — иначе Service и workload схлопнутся."""
    declared = _declared_constraint_names()
    assert "uq_kg_service_ns_name_kind" in declared
    assert "uq_kg_service_ns_name" not in declared, (
        "старый двухколоночный ключ вернулся — Service и workload снова "
        "станут одной строкой"
    )


@pytest.mark.parametrize("module", [
    "app/knowledge_graph/populator.py",
    "app/knowledge_graph/kg_sync.py",
])
def test_both_upsert_copies_set_node_kind(module: str):
    """Обе копии upsert-логики кладут node_kind в values.

    Их две, и об этом легко забыть — именно так и произошло. Без node_kind в
    values конфликт по трёхколоночному ключу опирается на server_default, что
    работает, но молча ломается при любом изменении дефолта.
    """
    src = pathlib.Path(module).read_text(encoding="utf-8")
    if "pg_insert(Service.__table__)" not in src:
        pytest.skip(f"{module} не делает pg-upsert сервисов")
    assert re.search(r'"node_kind"\s*:', src), (
        f"{module}: values для pg_insert не содержит node_kind"
    )
