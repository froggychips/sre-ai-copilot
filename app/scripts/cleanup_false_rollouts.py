"""Убрать ложные «деплои», записанные kg_deploy_watch по росту generation.

Первая версия watcher'а (1.0.4, 05.09.2026) считала выкатом любой рост
`metadata.generation`. Он растёт при ЛЮБОМ изменении spec, включая
`replicas` — то есть на каждом тике HPA. За 40 минут после деплоя в
`kg_deployments` приехало 6968 записей `attribution=k8s_rollout,
rollout_reason=generation` на 776 сервисов; образы не менялись ни у одного
(`images == previous_images`). `RecentDeployRule` верила каждой.

Критерий исправлен (образы + хэш `spec.template`), но записи остались, и
час после этого любой алерт на автоскейлящемся сервисе получал «недавний
деплой» в обогащении.

Удаляем ровно то, что доказуемо ложно: `k8s_rollout` + `rollout_reason =
'generation'` + образы не изменились. Записи `rollout_reason='image'`
(их за прогон не было, но правило общее) не трогаем.

CLI:
    python -m app.scripts.cleanup_false_rollouts            # dry-run
    python -m app.scripts.cleanup_false_rollouts --apply    # удалить
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict

from sqlalchemy import text

from app.database import SessionLocal

_SELECT = """
SELECT id FROM kg_deployments
WHERE buildtype_id = 'k8s_rollout'
  AND (extras->>'rollout_reason') = 'generation'
  AND (extras->>'images') = (extras->>'previous_images')
"""

_BATCH = 2000


def cleanup_false_rollouts(db, apply: bool = False) -> Dict[str, Any]:
    ids = [row[0] for row in db.execute(text(_SELECT)).fetchall()]
    stats: Dict[str, Any] = {"candidates": len(ids), "deleted": 0, "applied": False}
    if not apply or not ids:
        return stats
    for offset in range(0, len(ids), _BATCH):
        chunk = ids[offset:offset + _BATCH]
        res = db.execute(
            text("DELETE FROM kg_deployments WHERE id = ANY(:ids)"), {"ids": chunk},
        )
        stats["deleted"] += res.rowcount or 0
        db.commit()
    stats["applied"] = True
    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    db = SessionLocal()
    try:
        result = cleanup_false_rollouts(db, apply=args.apply)
    finally:
        db.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not args.apply:
        print("\ndry-run: --apply чтобы удалить.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
