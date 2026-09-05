"""D2-auto CLI: ручной запуск drift cleanup.

В прод idempotent — beat task `kg_drift_cleanup` запускает каждый час
автоматически с safety threshold. Этот скрипт для:
  - manual override threshold (--max-drift 100)
  - dry-run preview (без --apply)
  - разовый прогон после долгого простоя чистки
  - debug в IDE

Порог сравнивается с УСАДКОЙ live-набора namespace, а не с долей
накопленного мусора: последняя растёт оттого, что чистка не идёт, и в роли
предохранителя блокировала сама себя. Помечаются только ns, чьё отсутствие
подтверждено `kg_namespaces` дольше grace-периода, — `--max-drift` этой
проверки не отменяет.

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
                        help="макс. усадка live-набора ns в %%, выше — abort "
                             "(default 20)")
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
            f"\n⚠️  skipped: live-набор ns усох на {result['shrink_pct']}% "
            f"относительно активного в графе (порог {args.max_drift}%). "
            "Похоже на сбой `kubectl get ns`, а не на снос стендов — "
            "проверь доступность кластера, прежде чем поднимать --max-drift."
        )
        return 1
    if result.get("unconfirmed_ns"):
        print(
            f"\nждут подтверждения lifecycle: {len(result['unconfirmed_ns'])} ns "
            "(пропали из кластера, но ещё не выдержали grace-период)"
        )
    if not args.apply:
        print("\ndry-run: --apply чтобы применить.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
