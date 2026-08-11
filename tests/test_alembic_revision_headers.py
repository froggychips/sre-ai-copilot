"""Docstring-заголовок миграции обязан совпадать с фактическими revision'ами.

Зачем guard. Alembic читает `revision` / `down_revision`, а человек в
инциденте читает шапку файла — `Revision ID:` / `Revises:`. Когда они
разъезжаются (найдено при ревью: `20260807_0400_kg_idempotency_constraints`
объявлял себя `20260807_0200` и ревизил несуществующий `20260807_0100`,
`20260807_0300` ревизил `20260610_0100` вместо `20260807_0200`), оператор
на откате берёт цель из шапки и делает `alembic downgrade` не туда:
в лучшем случае «Can't locate revision», в худшем — снимает лишнюю
миграцию (drop колонки = потеря данных).

Заодно проверяем саму цепочку: один корень, одна голова, без развилок —
две головы Alembic вообще отказывается апгрейдить (`Multiple heads`), и
узнать об этом на прод-деплое дорого.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"


def _revision_files() -> list[Path]:
    files = sorted(p for p in VERSIONS_DIR.glob("*.py") if not p.name.startswith("_"))
    assert files, f"не найдено ни одной миграции в {VERSIONS_DIR}"
    return files


def _parse(path: Path) -> dict:
    """Достаём docstring и литералы revision/down_revision без импорта модуля.

    Именно ast, а не import: модуль миграции на импорте тянет alembic.op и
    в разных версиях объявляет переменные то с аннотацией (`revision: str`),
    то без — ast покрывает оба варианта и ничего не исполняет.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    doc = ast.get_docstring(tree) or ""
    values: dict = {}
    for node in tree.body:
        target = None
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        if target in ("revision", "down_revision"):
            values[target] = ast.literal_eval(node.value)

    def _header(field: str) -> str | None:
        # Пустое значение (`Revises:` у initial) = None, поля нет вовсе = ""
        match = re.search(rf"^{field}:[ \t]*(\S*)[ \t]*$", doc, re.M)
        if match is None:
            return ""
        return match.group(1) or None

    return {
        "revision": values.get("revision"),
        "down_revision": values.get("down_revision"),
        "doc_revision": _header("Revision ID"),
        "doc_down_revision": _header("Revises"),
    }


def test_docstring_header_matches_actual_revision_ids():
    """`Revision ID:` / `Revises:` в шапке == фактическим значениям в коде."""
    drift = []
    for path in _revision_files():
        info = _parse(path)
        if info["doc_revision"] != info["revision"]:
            drift.append(
                f"{path.name}: шапка Revision ID={info['doc_revision']!r}, "
                f"в коде revision={info['revision']!r}"
            )
        if info["doc_down_revision"] != info["down_revision"]:
            drift.append(
                f"{path.name}: шапка Revises={info['doc_down_revision']!r}, "
                f"в коде down_revision={info['down_revision']!r}"
            )
    assert not drift, (
        "шапка миграции врёт про ревизии — оператор откатит не туда:\n  "
        + "\n  ".join(drift)
    )


def test_revision_id_matches_filename_prefix():
    """Имя файла начинается с собственного revision id.

    Без этого `ls alembic/versions` перестаёт быть картой цепочки, а именно
    по ней и ориентируются в инциденте.
    """
    mismatched = [
        f"{path.name} != revision {_parse(path)['revision']!r}"
        for path in _revision_files()
        if not path.name.startswith(f"{_parse(path)['revision']}_")
    ]
    assert not mismatched, f"имя файла не совпадает с revision: {mismatched}"


def test_revision_chain_is_linear_single_head():
    """Цепочка ревизий: один корень, одна голова, без развилок и дублей."""
    parsed = {path.name: _parse(path) for path in _revision_files()}
    revisions = [info["revision"] for info in parsed.values()]

    dupes = {r for r in revisions if revisions.count(r) > 1}
    assert not dupes, f"дублирующиеся revision id: {sorted(dupes)}"

    known = set(revisions)
    downs = [info["down_revision"] for info in parsed.values()]

    dangling = {
        f"{name} -> {info['down_revision']!r}"
        for name, info in parsed.items()
        if info["down_revision"] is not None and info["down_revision"] not in known
    }
    assert not dangling, f"down_revision указывает на несуществующую ревизию: {dangling}"

    roots = [r for r, d in zip(revisions, downs) if d is None]
    assert len(roots) == 1, f"корней должно быть ровно 1, найдено: {roots}"

    heads = sorted(known - {d for d in downs if d is not None})
    assert len(heads) == 1, f"Alembic не апгрейдит multiple heads, найдено: {heads}"

    forks = sorted({d for d in downs if d is not None and downs.count(d) > 1})
    assert not forks, f"развилка — на этих ревизиях висит >1 потомка: {forks}"


def test_env_guard_survives_python_optimize():
    """Guard на видимость таблиц в alembic/env.py — не `assert`.

    Под `python -O` / `PYTHONOPTIMIZE=1` (легко прилетает из образа или env
    пода) assert вырезается компилятором, и защита от ложных `drop_table` в
    autogenerate исчезает ровно в проде.
    """
    env_py = (VERSIONS_DIR.parent / "env.py").read_text(encoding="utf-8")
    guard = env_py.split("_visible = ")[1]
    assert "assert " not in guard, (
        "guard на target_metadata снова на assert — исчезнет под python -O"
    )
    assert "raise" in guard, "guard должен падать явным raise"
