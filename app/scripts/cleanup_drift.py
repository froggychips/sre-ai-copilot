"""D2-auto CLI: ручной запуск drift cleanup.

В прод idempotent — beat task `kg_drift_cleanup` запускает каждый час
автоматически с safety threshold. Этот скрипт для:
  - manual override threshold (--max-drift 100)
  - dry-run preview (без --apply)
  - debug в IDE

CLI:
    python -m app.scripts.cleanup_drift                    # dry-run, threshold 20%
    python -m app.scripts.cleanup_drift --apply            # реально пишет
    python -m app.scripts.cleanup_drift --apply --max-drift 50.0  # широкий threshold
"""
import argparse
import json
import sys

from app.database import SessionLocal
from app.knowledge_graph.drift_cleanup import run_drift_cleanup


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--max-drift", type=float, default=20.0,
                        help="threshold %% drift, выше — abort (default 20)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        result = run_drift_cleanup(
            db, max_drift_pct=args.max_drift, apply=args.apply,
        )
    finally:
        db.close()

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    if result.get("skipped_threshold"):
        print(
            f"\n⚠️  skipped: drift {result['drift_pct']}% > threshold "
            f"{args.max_drift}%. Override через --max-drift или manually."
        )
        return 1
    if not args.apply:
        print("\ndry-run: --apply чтобы применить.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
