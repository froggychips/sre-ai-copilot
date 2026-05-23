"""A5: бэкфилл kg_deployments из TC за расширенное окно (по умолчанию 30 дней).

`tc_deploys_to_kg` cron-task ограничен lookback=24h и limit=200 — это окно
для текущей жизни. Для регрессионных запросов («какой коммит сломал
сервис X неделю назад») нужна история. record_deployment идемпотентен
по (service_id, buildtype_id, build_number), повторы безопасны.

CLI:
    python -m app.scripts.backfill_tc_deploys              # default 30 дней
    python -m app.scripts.backfill_tc_deploys --days 7
    python -m app.scripts.backfill_tc_deploys --days 60 --limit 1000
"""
import argparse
import asyncio
import sys
from datetime import datetime

from app.database import SessionLocal
from app.knowledge_graph.populator import record_deployment
from app.knowledge_graph.schema import Service
from app.services.teamcity_service import branch_for_namespace, recent_deploys


async def _backfill(days: int, limit: int) -> dict:
    lookback_hours = days * 24
    print(f"fetching TC builds: lookback={days}д ({lookback_hours}h), limit={limit}")
    builds = await recent_deploys(lookback_hours=lookback_hours, limit=limit)
    print(f"  → fetched {len(builds)} builds")
    if not builds:
        return {"builds_fetched": 0, "kg_deployments_added": 0}

    db = SessionLocal()
    added = 0
    skipped_no_branch = 0
    skipped_no_time = 0
    try:
        all_ns = [ns for (ns,) in db.query(Service.namespace).distinct().all()]
        ns_by_branch: dict[str, list[str]] = {}
        for ns in all_ns:
            br = branch_for_namespace(ns)
            if br:
                ns_by_branch.setdefault(br, []).append(ns)

        for b in builds:
            branch_full = (b.get("branch") or "").replace("refs/heads/", "")
            target_namespaces = ns_by_branch.get(branch_full, [])
            if not target_namespaces:
                skipped_no_branch += 1
                continue

            finished = b.get("finished_at")
            started = b.get("started_at") or b.get("finished_at")
            if not started:
                skipped_no_time += 1
                continue
            try:
                started_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
            except ValueError:
                skipped_no_time += 1
                continue
            started_naive = started_dt.replace(tzinfo=None)
            finished_naive = None
            if finished:
                try:
                    finished_naive = datetime.fromisoformat(
                        finished.replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                except ValueError:
                    pass

            # SHA коммита из TC revisions[0]; multi-root monorepo — полный
            # список в extras['all_revisions']. NULL ок (manual trigger / no VCS).
            sha = b.get("sha")
            all_revisions = b.get("all_revisions") or []

            for ns in target_namespaces:
                ns_services = (
                    db.query(Service).filter_by(namespace=ns, synthetic=False).all()
                )
                for svc in ns_services:
                    extras: dict = {
                        "branch": branch_full,
                        "buildtype_name": b.get("buildtype_name"),
                        "url": b.get("url"),
                        "namespace_scope": True,
                        "backfill": True,
                    }
                    if len(all_revisions) > 1:
                        extras["all_revisions"] = all_revisions
                    record_deployment(
                        db,
                        service=svc,
                        started_at=started_naive,
                        finished_at=finished_naive,
                        sha=sha,
                        buildtype_id=b.get("buildtype_id"),
                        build_number=str(b.get("number") or ""),
                        status=b.get("status"),
                        triggered_by=b.get("triggered_by"),
                        extras=extras,
                    )
                    added += 1
        db.commit()
    finally:
        db.close()

    return {
        "builds_fetched": len(builds),
        "kg_deployments_added": added,
        "skipped_no_branch_match": skipped_no_branch,
        "skipped_no_timestamp": skipped_no_time,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30,
                        help="lookback в днях (default 30)")
    parser.add_argument("--limit", type=int, default=1000,
                        help="max builds из TC (default 1000)")
    args = parser.parse_args()

    result = asyncio.run(_backfill(days=args.days, limit=args.limit))
    print()
    for k, v in result.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
