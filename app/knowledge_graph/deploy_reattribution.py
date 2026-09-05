"""Переатрибуция kg_deployments: деплой должен указывать на своё окружение.

Записи, сделанные до фикса атрибуции, привязаны к namespace по VCS-ветке. У
deploy-конфигов ветка — литеральный `<default>`, и прежняя нормализация
`<default>` → `preprod` была догадкой. С ростом числа сквадов она стала
систематически неверной.

Замер на проде 22.08.2026:

    уникальных билдов в kg_deployments            441
    записей                                   124 911  (≈596 на билд)
    BuildAndDeploy #2917 деплоил squad-1, записан на preprod-* и squad-gd-*,
    сервисов squad-1 среди 596 записей — ни одного

Настоящая цель лежит в параметрах билда TeamCity: `NAMESPACE` есть у всех
deploy-конфигов, `SERVICE_NAME` — у тех, что деплоят один сервис. Этот
модуль дозапрашивает их и приводит записи в соответствие.

Что делает с записью:

  * цель известна и запись в правильном namespace → оставляет;
  * цель известна, запись в чужом → удаляет;
  * целевой сервис задан и запись не на него → удаляет;
  * целевой сервис задан и запись на него → снимает маркер
    `namespace_scope`: это доказательство деплоя ЭТОГО сервиса, а не
    «в namespace что-то каталось»;
  * записи для правильных сервисов, которых нет → создаёт.

Чего НЕ делает:

  * не трогает билды, для которых TeamCity не отдал параметры. Билд мог
    выпасть из retention, и тогда правильная цель неизвестна. Удалять по
    незнанию нельзя: отсутствие записи — тоже утверждение, просто другое;
  * не трогает записи без маркера `namespace_scope`. Их писали не
    broadcast'ом, и переатрибутировать их не за что.

Безопасность: dry-run по умолчанию, батчи с коммитом, отчёт по каждому
классу решений. Идемпотентно — повторный прогон на исправленных данных
ничего не меняет.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import (Any, Dict, List, Optional, Set, Tuple, cast)

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.knowledge_graph.populator import record_deployment
from app.knowledge_graph.schema import Deployment, Service

log = logging.getLogger(__name__)

__all__ = ["reattribute_deployments", "fetch_build_target"]

#: Записей на коммит. Того же порядка, что в остальных чистках графа.
_BATCH = 200

#: Таймаут одного запроса к TC. Билдов порядка полутысячи, и вешаться на
#: каждом по тридцать секунд смысла нет.
_TC_TIMEOUT = 15.0


def fetch_build_target(
    buildtype_id: str, build_number: str,
) -> Optional[Tuple[Optional[str], Optional[str]]]:
    """`(realm, service)` из параметров билда, или None если TC его не знает.

    None означает «спросить не удалось» и отличается от `(None, None)` —
    «билд есть, но параметров цели у него нет». Первое запрещает любые
    выводы, второе просто оставляет запись в покое.
    """
    if not settings.TC_URL or not settings.TC_TOKEN:
        return None
    locator = f"buildType:(id:{buildtype_id}),number:{build_number}"
    try:
        r = httpx.get(
            f"{settings.TC_URL.rstrip('/')}/app/rest/builds",
            params={"locator": locator,
                    "fields": "build(id,number,properties(property(name,value)))"},
            headers={"Authorization": f"Bearer {settings.TC_TOKEN}",
                     "Accept": "application/json"},
            timeout=_TC_TIMEOUT,
        )
        r.raise_for_status()
        builds = (r.json() or {}).get("build") or []
    except Exception as e:  # noqa: BLE001 — недоступность TC не повод портить данные
        log.warning("deploy_reattribution.tc_failed bt=%s num=%s err=%s",
                    buildtype_id, build_number, e)
        return None
    if not builds:
        return None
    props = {
        p.get("name"): p.get("value")
        for p in ((builds[0].get("properties") or {}).get("property") or [])
        if p.get("name")
    }
    return (props.get("NAMESPACE") or None, props.get("SERVICE_NAME") or None)


def _namespaces_of_realm(all_ns: List[str], realm: str) -> List[str]:
    """Namespace графа, принадлежащие реальму.

    Та же проверка, что в `tc_deploys_to_kg`: `squad-270` не должен попасть
    в `squad-27`.
    """
    prefix = f"{realm}-"
    return [ns for ns in all_ns if ns == realm or ns.startswith(prefix)]


def reattribute_deployments(
    db: Session, apply: bool = False, limit_builds: Optional[int] = None,
) -> Dict[str, Any]:
    """Привести атрибуцию kg_deployments в соответствие с целью билдов."""
    stats: Dict[str, Any] = {
        "builds_seen": 0, "builds_resolved": 0, "builds_unknown": 0,
        "builds_without_target": 0,
        "rows_deleted": 0, "rows_created": 0, "rows_kept": 0,
        "rows_unmarked": 0,
        "applied": False,
    }

    # От свежих к старым. Порядок здесь не косметика: TeamCity помнит билды
    # ограниченное время, и параметры доступны только у свежих. Проба
    # 22.08.2026 с алфавитной сортировкой начала с AssetLockBuildAndDeploy #28
    # и AirflowBuildAndUpdate #35 — из первых пятнадцати билдов четырнадцать
    # оказались неизвестны TC, и прогон выглядел бесполезным, хотя для свежих
    # цель определяется прекрасно.
    #
    # `--limit-builds` при таком порядке отсекает старый хвост, а не свежую
    # голову, то есть делает именно то, что от него ждут.
    build_keys: List[Tuple[str, str]] = [
        (bt, num) for (bt, num, _last) in
        db.query(Deployment.buildtype_id, Deployment.build_number,
                 func.max(Deployment.started_at).label("last"))
        .filter(Deployment.buildtype_id.isnot(None),
                Deployment.build_number.isnot(None))
        .group_by(Deployment.buildtype_id, Deployment.build_number)
        .order_by(func.max(Deployment.started_at).desc())
        .all()
    ]
    if limit_builds:
        build_keys = build_keys[:limit_builds]

    all_ns: List[str] = [
        ns for (ns,) in db.query(Service.namespace).distinct().all() if ns
    ]

    for buildtype_id, build_number in build_keys:
        stats["builds_seen"] += 1
        target = fetch_build_target(buildtype_id, build_number)
        if target is None:
            # TC не ответил или билд выпал из retention: правильная цель
            # неизвестна, и трогать записи нельзя.
            stats["builds_unknown"] += 1
            continue
        realm, service_name = target
        if not realm:
            stats["builds_without_target"] += 1
            continue
        stats["builds_resolved"] += 1

        good_ns = set(_namespaces_of_realm(all_ns, realm))
        if not good_ns:
            # Реальм известен, но его namespace в графе нет. Удалять чужие
            # записи было бы правильно, а создать взамен нечего — оставляем
            # как есть, чтобы не превращать неверную историю в пустую.
            log.info("deploy_reattribution.realm_not_in_graph realm=%s bt=%s num=%s",
                     realm, buildtype_id, build_number)
            continue

        rows = (
            db.query(Deployment)
            .filter(Deployment.buildtype_id == buildtype_id,
                    Deployment.build_number == build_number)
            .all()
        )
        # Записи вне broadcast'а не переатрибутируем: их писали точечно.
        rows = [
            r for r in rows
            if isinstance(r.extras, dict) and r.extras.get("namespace_scope")
        ]
        if not rows:
            continue

        template = rows[0]
        wrong: List[Deployment] = []
        present: Set[Tuple[str, str]] = set()
        for r in rows:
            svc = db.get(Service, r.service_id)
            if svc is None:
                wrong.append(r)
                continue
            ns_ok = svc.namespace in good_ns
            svc_ok = (service_name is None) or (svc.name == service_name)
            if ns_ok and svc_ok:
                present.add((str(svc.namespace), str(svc.name)))
                stats["rows_kept"] += 1
                # Билд деплоил один сервис, и запись стоит именно на нём —
                # значит это доказательство ЕГО деплоя, а не «в namespace
                # что-то каталось». Пока маркер висит, `stale_classifier`
                # обязан считать запись broadcast'ом и не может выдать
                # `active`.
                if service_name and r.extras.get("namespace_scope"):
                    stats["rows_unmarked"] += 1
                    if apply:
                        # JSON-колонку переприсваиваем целиком: мутацию dict
                        # на месте SQLAlchemy не увидит и UPDATE не сделает.
                        updated: Dict[str, Any] = dict(r.extras)
                        updated["namespace_scope"] = False
                        r.extras = updated  # type: ignore[assignment]
            else:
                wrong.append(r)

        # Чего не хватает: сервисы правильного реальма без записи.
        want_q = db.query(Service).filter(Service.namespace.in_(good_ns),
                                          Service.synthetic.is_(False))
        if service_name:
            want_q = want_q.filter(Service.name == service_name)
        missing = [
            s for s in want_q.all()
            if (str(s.namespace), str(s.name)) not in present
        ]

        stats["rows_deleted"] += len(wrong)
        stats["rows_created"] += len(missing)
        if not apply:
            continue

        for offset in range(0, len(wrong), _BATCH):
            for r in wrong[offset:offset + _BATCH]:
                db.delete(r)
            db.commit()

        for offset in range(0, len(missing), _BATCH):
            for svc in missing[offset:offset + _BATCH]:
                extras = dict(template.extras or {})
                extras["reattributed_from_branch"] = extras.get("branch")
                extras["attribution"] = "build_param"
                # Шаблон взят с broadcast-записи и несёт её маркер. Для
                # билда с известным целевым сервисом копировать его нельзя —
                # создаём ровно то доказательство, которого не хватало.
                extras["namespace_scope"] = service_name is None
                try:
                    record_deployment(
                        db, service=svc,
                        started_at=cast(datetime, template.started_at),
                        finished_at=cast(Optional[datetime], template.finished_at),
                        sha=cast(Optional[str], template.sha),
                        repo=cast(Optional[str], template.repo),
                        buildtype_id=buildtype_id, build_number=build_number,
                        status=cast(Optional[str], template.status),
                        triggered_by=cast(Optional[str], template.triggered_by),
                        extras=extras,
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "deploy_reattribution.create_failed ns=%s svc=%s: %s",
                        svc.namespace, svc.name, e,
                    )
            db.commit()

    if apply:
        db.commit()
        stats["applied"] = True
    log.info("deploy_reattribution.done %s", stats)
    return stats
