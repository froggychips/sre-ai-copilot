"""Одноразовый бэкфилл team_owner для kg_services по новому regex.

    python -m app.scripts.backfill_team_owner            # dry-run по умолчанию
    python -m app.scripts.backfill_team_owner --apply    # реально пишет
"""
import argparse
import sys

from app.database import SessionLocal
from app.knowledge_graph.kg_sync import _derive_team_owner
from app.knowledge_graph.schema import Service


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="реально записать UPDATE; без флага — dry-run")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        rows = db.query(Service).filter(Service.team_owner.is_(None)).all()
        plan: list[tuple[int, str, str, str]] = []
        for s in rows:
            ns = str(s.namespace)
            derived = _derive_team_owner(ns)
            if derived:
                plan.append((int(s.id), ns, str(s.name), derived))

        by_team: dict[str, int] = {}
        for _, _, _, t in plan:
            by_team[t] = by_team.get(t, 0) + 1

        print(f"services без owner: {len(rows)}")
        print(f"которые покроет regex: {len(plan)} ({len(rows) - len(plan)} останутся None)")
        print("распределение по team_owner:")
        for t in sorted(by_team):
            print(f"  {t}: {by_team[t]}")

        if not args.apply:
            print("\ndry-run: ничего не записано. --apply чтобы применить.")
            return 0

        for sid, _, _, derived in plan:
            db.query(Service).filter(Service.id == sid).update(
                {"team_owner": derived}, synchronize_session=False,
            )
        db.commit()
        print(f"\nUPDATE применён: {len(plan)} строк.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
