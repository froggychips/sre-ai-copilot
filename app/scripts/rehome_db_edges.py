"""Перевес рёбер `uses_db` на db-узлы живых окружений.

Разгребает последствия отключённого `phantom_db_cleanup`, который схлопывал
разные физические базы разных окружений в одну — см. docstring модуля
`app.knowledge_graph.db_edge_rehome`.

Dry-run по умолчанию; запись только с --apply.

Перенос обратим: прежний адрес пишется в `extras.rehomed_from`, и `--undo`
читает именно его. Узел мог быть удалён за это время — такое ребро
остаётся на месте и попадает в счётчик `target_gone`.

CLI:
    python -m app.scripts.rehome_db_edges                 # отчёт
    python -m app.scripts.rehome_db_edges --apply         # перенести
    python -m app.scripts.rehome_db_edges --undo          # отчёт отката
    python -m app.scripts.rehome_db_edges --undo --apply  # вернуть обратно
"""
import argparse
import json
import sys

from app.database import SessionLocal
from app.knowledge_graph.db_edge_rehome import rehome_db_edges, undo_rehome


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="реально перенести рёбра (без флага — dry-run)")
    parser.add_argument("--undo", action="store_true",
                        help="вернуть перенесённые рёбра по журналу в extras")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.undo:
            result = undo_rehome(db, apply=args.apply)
        else:
            result = rehome_db_edges(db, apply=args.apply)
    finally:
        db.close()

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    if not args.apply and result.get("marked_edges"):
        print(
            f"\n\u2139\ufe0f  dry-run отката: {result['marked_edges']} рёбер помечены "
            "как перенесённые. Запусти с --undo --apply.",
            file=sys.stderr,
        )
    if not args.apply and result.get("stale_edges"):
        print(
            f"\nℹ️  dry-run: {result['stale_edges']} рёбер ведут в базы "
            f"удалённых окружений, приёмник найден для {result['repointed']}"
            + (f", без приёмника {result['no_target']}"
               if result.get("no_target") else "")
            + ". Запусти с --apply.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
