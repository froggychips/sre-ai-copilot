"""Разовый backfill: схлопывание фантомных db-узлов (C2). См. модуль
`app.knowledge_graph.phantom_db_cleanup`.

#185 остановил рост фантомов going-forward; это — чистка уже накопленного.
Dry-run по умолчанию; реальная запись только с --apply.

CLI:
    python -m app.scripts.cleanup_phantom_db_nodes          # dry-run (отчёт)
    python -m app.scripts.cleanup_phantom_db_nodes --apply  # схлопнуть
"""
import argparse
import json
import sys

from app.database import SessionLocal
from app.knowledge_graph.phantom_db_cleanup import collapse_phantom_db_nodes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="реально схлопнуть (без флага — dry-run)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        result = collapse_phantom_db_nodes(db, apply=args.apply)
    finally:
        db.close()

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    if not args.apply and result.get("nodes_to_delete"):
        print(
            f"\nℹ️  dry-run: {result['total_db_nodes']} db-узлов → "
            f"{result['distinct_db_names']} канонических "
            f"(удалится {result['nodes_to_delete']}). Запусти с --apply.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
