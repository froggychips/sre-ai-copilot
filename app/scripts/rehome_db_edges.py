"""Перевес рёбер `uses_db` на db-узлы живых окружений.

Разгребает последствия отключённого `phantom_db_cleanup`, который схлопывал
разные физические базы разных окружений в одну — см. docstring модуля
`app.knowledge_graph.db_edge_rehome`.

Dry-run по умолчанию; запись только с --apply.

CLI:
    python -m app.scripts.rehome_db_edges          # отчёт, ничего не пишет
    python -m app.scripts.rehome_db_edges --apply  # перенести
"""
import argparse
import json
import sys

from app.database import SessionLocal
from app.knowledge_graph.db_edge_rehome import rehome_db_edges


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="реально перенести рёбра (без флага — dry-run)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        result = rehome_db_edges(db, apply=args.apply)
    finally:
        db.close()

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
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
