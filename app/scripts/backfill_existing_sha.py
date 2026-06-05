"""One-off backfill: для каждого kg_deployments где sha IS NULL запросить
TC build details и UPDATE sha (если есть revisions).

Запуск:
  python -m app.scripts.backfill_existing_sha [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
from typing import Any

from sqlalchemy import text

from app.database import SessionLocal
from app.vendor.teamcity_mcp.client import TCError, TeamCityClient

logger = logging.getLogger(__name__)


_FIELDS = "revisions(revision(version,vcs-root-instance(name,vcs-root-id)))"


def _build_id_from_extras(extras: Any) -> str | None:
    """TC build id из extras.url (?buildId=<N>) — для надёжного id-локатора."""
    if not extras:
        return None
    if isinstance(extras, str):
        try:
            extras = json.loads(extras)
        except Exception:
            return None
    m = re.search(r"buildId=(\d+)", (extras or {}).get("url") or "")
    return m.group(1) if m else None


def _fetch_sha(client: TeamCityClient, locator: str) -> tuple[str | None, list[dict]]:
    """Вернёт (sha, all_revisions). Если revisions пусто или 404 — (None, [])."""
    try:
        data = client.get_json(f"/app/rest/builds/{locator}", params={"fields": _FIELDS})
    except TCError as e:
        if e.status == 404:
            return None, []
        raise
    revs = (data.get("revisions") or {}).get("revision") or []
    if not revs:
        return None, []
    primary = revs[0].get("version") or None
    all_rev = [
        {
            "sha": r.get("version"),
            "root": ((r.get("vcs-root-instance") or {}).get("name")),
            "vcs_root_id": ((r.get("vcs-root-instance") or {}).get("vcs-root-id")),
        }
        for r in revs if r.get("version")
    ]
    return primary, all_rev


def run(limit: int = 0, dry_run: bool = False, sleep_sec: float = 0.05) -> dict:
    client = TeamCityClient()
    updated = skipped_no_rev = errors = 0
    with SessionLocal() as db:
        sql = """
            SELECT id, buildtype_id, build_number, extras
            FROM kg_deployments
            WHERE sha IS NULL
              AND buildtype_id IS NOT NULL
              AND build_number IS NOT NULL
            ORDER BY started_at DESC
        """
        if limit and limit > 0:
            sql += f" LIMIT {int(limit)}"
        rows = db.execute(text(sql)).fetchall()
        total = len(rows)
        logger.info("backfill_existing_sha.start", extra={"candidates": total, "dry_run": dry_run})
        print(f"candidates: {total}, dry_run={dry_run}")

        for i, (dep_id, bt, num, extras) in enumerate(rows, 1):
            # id-локатор надёжнее number: number:<N> у branched/FAILURE-билдов
            # отдаёт 404 (поиск по default-branch). build id берём из
            # extras.url (?buildId=); fallback — number + branch:any,state:any.
            build_id = _build_id_from_extras(extras)
            locator = (
                f"id:{build_id}" if build_id
                else f"buildType:{bt},number:{num},branch:(default:any),state:any"
            )
            try:
                sha, all_rev = _fetch_sha(client, locator)
            except Exception as e:
                errors += 1
                if errors <= 5:
                    logger.warning("backfill.fetch_failed", extra={"id": dep_id, "bt": bt, "num": num, "err": str(e)[:120]})
                time.sleep(sleep_sec)
                continue
            if not sha:
                skipped_no_rev += 1
            elif not dry_run:
                # UPDATE: sha + extras.all_revisions если их >1
                if len(all_rev) > 1:
                    db.execute(text("""
                        UPDATE kg_deployments
                        SET sha = :sha,
                            extras = ((COALESCE(extras::jsonb, '{}'::jsonb)
                                       || jsonb_build_object('all_revisions', cast(:all_rev as jsonb)))::text)::json
                        WHERE id = :id AND sha IS NULL
                    """), {"sha": sha, "all_rev": __import__("json").dumps(all_rev), "id": dep_id})
                else:
                    db.execute(text(
                        "UPDATE kg_deployments SET sha = :sha WHERE id = :id AND sha IS NULL"
                    ), {"sha": sha, "id": dep_id})
                updated += 1
                if updated % 200 == 0:
                    db.commit()
                    print(f"  ...committed at {updated}/{total}")
            else:
                updated += 1  # would-update count
            time.sleep(sleep_sec)
        if not dry_run:
            db.commit()

    summary = {
        "total_candidates": total,
        "updated": updated,
        "skipped_no_rev": skipped_no_rev,
        "errors": errors,
    }
    logger.info("backfill_existing_sha.done", extra=summary)
    print("DONE:", summary)
    return summary


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=0, help="0 = all")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--sleep", type=float, default=0.05, help="seconds between TC requests")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run(limit=args.limit, dry_run=args.dry_run, sleep_sec=args.sleep)


if __name__ == "__main__":
    main()
