"""Индекс не должен повторять работу соседнего индекса.

Замер на проде 05.09.2026: около 1.7 ГБ индексов, ни один из которых не
отвечает на вопрос, на который уже не отвечает другой индекс той же таблицы.
Половина базы (6 ГБ) — индексы, и большая их часть — копии.

Все три источника — в объявлениях моделей, то есть возвращаются одной
строчкой невнимательности:

  * `primary_key=True, index=True` — PK уже создаёт уникальный индекс, и
    `index=True` кладёт рядом второй. Было в 15 таблицах из 16; на
    `kg_service_health` это 423 МБ;
  * `Index(...)` тем же составом колонок, что и `UniqueConstraint(...)` —
    928 МБ на той же таблице;
  * `index=True` на колонке, которая уже стоит первой в составном индексе.

Цена не только в месте: каждый индекс обновляется на каждой вставке, а в
`kg_service_health` приезжает 377 тысяч строк в сутки.

Тест смотрит на метаданные SQLAlchemy, а не на живую базу: он должен падать
в момент правки модели, а не через месяц на проде.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

import pytest

from app.database import Base
# Импорт ради регистрации моделей в Base.metadata — таблицы объявлены в этих
# модулях, и без импорта metadata пустая.
import app.knowledge_graph.schema  # noqa: F401
import app.remediation.models  # noqa: F401


#: Индексы, которые формально покрыты другим, но оставлены намеренно.
#: Правило про префикс верно по возможностям, а не по цене: узкий b-tree
#: дешевле читать, чем широкий, и на горячем пути это перевешивает экономию.
#: Каждая запись — с замером, иначе список превратится в свалку исключений.
_DELIBERATE = {
    # 12,5 млн сканов каждый; 900 КБ против 2,4 МБ у покрывающего
    # uq_kg_edge_src_dst_kind_direction. Blast-radius обходит рёбра по одному
    # концу, и это самый горячий запрос графа.
    "ix_kg_service_edges_src_id",
    "ix_kg_service_edges_dst_id",
    # 825 тысяч сканов против нуля у uq_kg_volume_edge_src_dst_kind:
    # уникальный держит констрейнт, а читают через эти.
    "ix_kg_volume_edges_src",
    "ix_kg_volume_edges_dst",
}


def _kg_tables():
    return [t for name, t in sorted(Base.metadata.tables.items())
            if name.startswith("kg_")]


def _index_sets(table) -> List[Tuple[str, Tuple[str, ...]]]:
    """Все индексы таблицы как (имя, кортеж колонок в порядке объявления).

    Считаются и явные `Index`, и уникальные констрейнты, и первичный ключ:
    физически СУБД создаёт b-tree для каждого из них, и покрытие одного
    другим не зависит от того, как он объявлен.
    """
    out: List[Tuple[str, Tuple[str, ...]]] = []
    pk_cols = tuple(c.name for c in table.primary_key.columns)
    if pk_cols:
        out.append((f"PRIMARY KEY {table.name}", pk_cols))
    for c in table.constraints:
        cols = tuple(col.name for col in getattr(c, "columns", []))
        name = getattr(c, "name", None)
        if cols and name and c is not table.primary_key:
            out.append((str(name), cols))
    for ix in table.indexes:
        out.append((ix.name, tuple(c.name for c in ix.columns)))
    return out


def _is_prefix(short: Tuple[str, ...], long: Tuple[str, ...]) -> bool:
    """b-tree по (a, b, c) обслуживает запросы по (a) и (a, b)."""
    return len(short) < len(long) and long[:len(short)] == short


@pytest.mark.parametrize("table", _kg_tables(), ids=lambda t: t.name)
def test_no_index_duplicates_another(table):
    """Ни один индекс не повторяет другой и не является его префиксом."""
    indexes = _index_sets(table)
    redundant: List[str] = []

    for name, cols in indexes:
        if name.startswith("PRIMARY KEY") or name.startswith("uq_"):
            continue  # констрейнты держат инварианты, а не только поиск
        if name in _DELIBERATE:
            continue
        for other_name, other_cols in indexes:
            if other_name == name:
                continue
            if cols == other_cols:
                # Точная копия. Оставляем ту, что держит констрейнт.
                if other_name.startswith(("PRIMARY KEY", "uq_")):
                    redundant.append(f"{name} == {other_name} {cols}")
                break
            if _is_prefix(cols, other_cols):
                redundant.append(f"{name} {cols} ⊂ {other_name} {other_cols}")
                break

    assert not redundant, (
        f"{table.name}: индексы дублируют друг друга — "
        + "; ".join(redundant)
        + ". Каждый из них обновляется на каждой вставке и занимает место, "
        "не отвечая ни на один вопрос, на который не отвечает покрывающий."
    )


def test_primary_key_columns_have_no_extra_index():
    """`primary_key=True, index=True` — всегда лишний индекс.

    Отдельным тестом, потому что это самый частый способ вернуть проблему:
    строчка выглядит как забота о производительности, а создаёт вторую копию
    того, что PK уже построил.
    """
    offenders: Dict[str, str] = {}
    for table in _kg_tables():
        pk_cols = tuple(c.name for c in table.primary_key.columns)
        for ix in table.indexes:
            if tuple(c.name for c in ix.columns) == pk_cols:
                offenders[table.name] = ix.name

    assert not offenders, (
        "индексы поверх PRIMARY KEY: "
        + ", ".join(f"{t} → {i}" for t, i in sorted(offenders.items()))
    )
