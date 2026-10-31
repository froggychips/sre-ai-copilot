"""Схлопнуть ns-broadcast сборок статики в одну строку на (билд, namespace).

До 31.10.2026 `tc_deploys_to_kg` раскладывал каждую сборку
`Wo_StaticsNewCluster_*` на все сервисы целевого namespace: 963 тыс. строк
kg_deployments из 1,24 млн при 1487 билдах (preprod-kingdom2/bot-service —
1258 «деплоев» за октябрь). Новый ингест пишет одну строку на синтетический
узел `statics` namespace (schema.STATICS_NODE_NAME); этот скрипт переносит
историю в ту же форму:

  1. заводит узел `statics` в каждом namespace, где есть такие строки;
  2. INSERT … SELECT DISTINCT ON (buildtype_id, build_number, namespace) —
     по одной строке на билд и namespace, поля берутся из любой из копий
     (они одинаковы: билд один), `ON CONFLICT DO NOTHING` делает шаг
     идемпотентным;
  3. удаляет broadcast-копии пачками по --batch, коммит после каждой.

Только PostgreSQL (DISTINCT ON, ON CONFLICT). По умолчанию dry-run: считает
и печатает, ничего не пишет. `--apply` — выполнить. DELETE место на диске не
возвращает: после прогона нужен `VACUUM (ANALYZE) kg_deployments`, а чтобы
отдать ~1,5 ГБ ОС — `VACUUM FULL` в окне (писатели: tc_deploys_to_kg каждые
15 мин и kg_deploy_watch — на время VACUUM FULL воркеры в 0).

    python -m app.scripts.collapse_statics_broadcast            # dry-run
    python -m app.scripts.collapse_statics_broadcast --apply
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Any, cast

from sqlalchemy import text

from app.database import SessionLocal
from app.knowledge_graph.populator import ensure_statics_node
from app.knowledge_graph.schema import STATICS_NODE_NAME
from app.services.teamcity_service import STATICS_BUILDTYPE_PREFIX

_LIKE = STATICS_BUILDTYPE_PREFIX + "%"

_COUNT_SQL = text(
    "SELECT count(*) AS rows, count(DISTINCT (d.buildtype_id, d.build_number)) AS builds, "
    "       count(DISTINCT s.namespace) AS namespaces, "
    "       count(DISTINCT (d.buildtype_id, d.build_number, s.namespace)) AS target_rows "
    "FROM kg_deployments d JOIN kg_services s ON s.id = d.service_id "
    "WHERE d.buildtype_id LIKE :like AND s.name <> :anchor"
)
_NAMESPACES_SQL = text(
    "SELECT DISTINCT s.namespace FROM kg_deployments d "
    "JOIN kg_services s ON s.id = d.service_id "
    "WHERE d.buildtype_id LIKE :like AND s.name <> :anchor"
)
_INSERT_SQL = text(
    "INSERT INTO kg_deployments (service_id, sha, repo, buildtype_id, build_number, "
    "                            started_at, finished_at, status, triggered_by, extras) "
    "SELECT DISTINCT ON (d.buildtype_id, d.build_number, s.namespace) "
    "       a.id, d.sha, d.repo, d.buildtype_id, d.build_number, "
    "       d.started_at, d.finished_at, d.status, d.triggered_by, d.extras "
    "FROM kg_deployments d "
    "JOIN kg_services s ON s.id = d.service_id "
    "JOIN kg_services a ON a.namespace = s.namespace AND a.name = :anchor "
    "                   AND a.node_kind = 'service' "
    "WHERE d.buildtype_id LIKE :like AND s.name <> :anchor "
    "ORDER BY d.buildtype_id, d.build_number, s.namespace, d.id "
    "ON CONFLICT (service_id, buildtype_id, build_number) DO NOTHING"
)
_DELETE_SQL = text(
    "DELETE FROM kg_deployments WHERE id IN ("
    "  SELECT d.id FROM kg_deployments d JOIN kg_services s ON s.id = d.service_id "
    "  WHERE d.buildtype_id LIKE :like AND s.name <> :anchor LIMIT :batch)"
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="выполнить (иначе dry-run)")
    ap.add_argument("--batch", type=int, default=50_000, help="размер пачки DELETE")
    args = ap.parse_args(argv)

    db = SessionLocal()
    try:
        if db.get_bind().dialect.name != "postgresql":
            print("Только PostgreSQL.", file=sys.stderr)
            return 2
        params = {"like": _LIKE, "anchor": STATICS_NODE_NAME}
        row = db.execute(_COUNT_SQL, params).one()
        print(
            f"broadcast-строк статики: {row.rows}, билдов: {row.builds}, "
            f"namespace: {row.namespaces}, строк после схлопывания: {row.target_rows}"
        )
        if row.rows == 0:
            print("Нечего схлопывать.")
            return 0
        if not args.apply:
            print("dry-run: ничего не изменено (добавь --apply).")
            return 0

        namespaces = [str(ns) for (ns,) in db.execute(_NAMESPACES_SQL, params).all()]
        for ns in namespaces:
            ensure_statics_node(db, ns)
        db.commit()
        print(f"узлы statics: {len(namespaces)} namespace")

        t0 = time.monotonic()
        inserted = cast(Any, db.execute(_INSERT_SQL, params)).rowcount
        db.commit()
        print(f"перенесено на якорь: {inserted} строк за {time.monotonic() - t0:.0f} с")

        deleted = 0
        t0 = time.monotonic()
        while True:
            n = cast(Any, db.execute(_DELETE_SQL, {**params, "batch": args.batch})).rowcount
            db.commit()
            deleted += n
            if n == 0:
                break
            print(f"  удалено {deleted} ({time.monotonic() - t0:.0f} с)", flush=True)
        print(f"удалено broadcast-строк: {deleted}")
        print("Дальше: VACUUM (ANALYZE) kg_deployments; VACUUM FULL — в окне с воркерами в 0.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
