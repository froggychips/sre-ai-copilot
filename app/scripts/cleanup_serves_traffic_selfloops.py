"""Разовый backfill: удаление `serves_traffic` self-loop рёбер. См. модуль
`app.knowledge_graph.serves_traffic_selfloop_cleanup`.

Guard в `k8s_topology_resources_sync` остановил рост self-loop'ов going-forward;
это — чистка уже накопленного. Dry-run по умолчанию; удаление только с --apply.

CLI:
    python -m app.scripts.cleanup_serves_traffic_selfloops          # dry-run
    python -m app.scripts.cleanup_serves_traffic_selfloops --apply  # удалить
"""
import argparse
import json
import sys

from app.database import SessionLocal
from app.knowledge_graph.serves_traffic_selfloop_cleanup import (
    delete_serves_traffic_self_loops,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="реально удалить (без флага — dry-run)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        result = delete_serves_traffic_self_loops(db, apply=args.apply)
    finally:
        db.close()

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    if not args.apply and result.get("self_loops_found"):
        print(
            f"\nℹ️  dry-run: {result['self_loops_found']} serves_traffic "
            f"self-loop рёбер. Запусти с --apply.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
