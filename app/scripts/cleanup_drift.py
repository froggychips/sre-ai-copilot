"""D2-mini: пометить как synthetic kg_services из namespace-ов которых нет в k8s.

Не удаляет данные — мягкий маркер: synthetic=True + extras в metadata_json.
Реверсивно через SQL UPDATE.

CLI:
    python -m app.scripts.cleanup_drift               # dry-run
    python -m app.scripts.cleanup_drift --apply       # реально пишет
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime

from app.database import SessionLocal
from app.knowledge_graph.schema import Service


def _k8s_namespaces() -> set[str]:
    """Список существующих в кластере ns."""
    out = subprocess.run(
        ["kubectl", "get", "ns", "-o", "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}"],
        capture_output=True, text=True, check=False,
    )
    if out.returncode != 0:
        raise RuntimeError(f"kubectl failed: {out.stderr.strip()}")
    return {ln.strip() for ln in out.stdout.splitlines() if ln.strip()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="реально записать UPDATE; без флага — dry-run")
    args = parser.parse_args()

    k8s_ns = _k8s_namespaces()
    print(f"k8s namespaces: {len(k8s_ns)}")

    db = SessionLocal()
    try:
        all_ns = {ns for (ns,) in db.query(Service.namespace).distinct().all()}
        drift_ns = sorted(all_ns - k8s_ns)
        print(f"KG namespaces без k8s соответствия: {len(drift_ns)}")
        for n in drift_ns:
            print(f"  - {n}")

        if not drift_ns:
            print("Drift не обнаружен.")
            return 0

        affected = (
            db.query(Service).filter(Service.namespace.in_(drift_ns)).all()
        )
        print(f"\nservices в drift-ns: {len(affected)}")
        for s in affected[:20]:
            cur_synth = "synthetic" if s.synthetic else "real"
            print(f"  - {s.namespace}/{s.name} (now: {cur_synth})")
        if len(affected) > 20:
            print(f"  ... ещё {len(affected) - 20}")

        if not args.apply:
            print("\ndry-run: ничего не записано. --apply чтобы применить.")
            return 0

        now_iso = datetime.utcnow().isoformat()
        updated = 0
        for s in affected:
            s.synthetic = True
            meta = dict(s.metadata_json or {})
            meta.setdefault("drift_marked_at", now_iso)
            meta.setdefault("drift_reason", "ns_not_in_k8s")
            s.metadata_json = meta
            updated += 1
        db.commit()
        print(f"\nUPDATE применён: {updated} services помечены synthetic=true (drift).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
