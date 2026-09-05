"""Второй, независимый источник деплоев: сам кластер.

`kg_deployments` наполнялся ровно из одного места — TeamCity. Цена такой
монополии видна в замере 05.09.2026:

    записей                                   137 423
    из них ns-broadcast                       137 423  (все)
    с точной целью (SERVICE_NAME в билде)          36  (0,03%)

То есть «какой сервис катился» знал только тот, кто не поленился проставить
параметр в конфиге билда. Остальные 99,97% записей — утверждение «в этом
namespace что-то каталось», разосланное всем его сервисам. На таком входе
`stale_classifier` не мог выдать `active` ни одному узлу, а
`RecentDeployRule` отвечала «деплой был» на любой алерт активного стенда.

Кластер знает точнее и не зависит от дисциплины заполнения параметров:
у каждого workload'а есть `metadata.generation`, который растёт при КАЖДОМ
изменении спеки, и образы контейнеров. Идея — из Coroot, который так же
отслеживает rollout'ы напрямую в Kubernetes, а не через интеграцию с CI.

**Что считается деплоем.** Смена набора образов или смена `spec.template`
(хэш). НЕ рост `metadata.generation`: он растёт при ЛЮБОМ изменении spec,
включая `replicas`, — то есть каждый тик HPA и каждый `kubectl scale`. Первая
версия этого модуля (1.0.4, 05.09.2026) брала generation как признак и за 40
минут записала 6968 ложных «деплоев» на 776 сервисов — образы при этом не
менялись ни у одного. `RecentDeployRule` верила каждому. Хэш `spec.template`
свободен от этого: replicas лежит в `spec`, а не в `spec.template`, и
меняется без изменения шаблона пода. Откат на предыдущий тег меняет образ —
это ровно тот случай, который знать и нужно.

**Первый прогон ничего не пишет.** Снимка нет — сравнивать не с чем, и
единственное, что можно сделать честно, это запомнить текущее состояние.
Иначе на старте в граф приехало бы 4360 фиктивных «деплоев», датированных
моментом запуска задачи.

**Куда пишется.** На узел `node_kind=service` — тот самый, который читает
`recent_deploys_for` (у workload-узла своя строка, и запись на неё
RecentDeployRule не увидела бы). Если одноимённого Service-узла нет,
пишем на workload — потерять событие хуже, чем записать его на соседа.
И главное: `namespace_scope=False`, потому что это доказательство деплоя
КОНКРЕТНОГО объекта, а не догадка по namespace.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import structlog
from sqlalchemy.orm import Session

from app.knowledge_graph.populator import record_deployment
from app.knowledge_graph.schema import (NODE_KIND_SERVICE, NODE_KIND_WORKLOAD,
                                        Service)

log = structlog.get_logger(__name__)

__all__ = ["watch_k8s_rollouts", "SNAPSHOT_KEY", "ATTRIBUTION"]

#: Redis-хэш «что мы видели в прошлый раз»: поле на workload.
SNAPSHOT_KEY = "kg:workload_revisions"

#: Провенанс записи. Рядом с `build_param` / `vcs_branch` из TeamCity —
#: по нему потребитель отличает наблюдение от догадки.
ATTRIBUTION = "k8s_rollout"

#: `buildtype_id` для записей этого источника. Дедуп `record_deployment`
#: идёт по (service_id, buildtype_id, build_number), а номером служит
#: `<uid>:<template_hash>` — повторное обнаружение того же шаблона пода не
#: создаёт второй строки, сколько бы раз задача ни прошла.
BUILDTYPE_ID = "k8s_rollout"

#: Сколько живёт снимок. Больше любого разумного простоя задачи и меньше
#: срока, за который состояние кластера успевает полностью смениться:
#: протухший снимок дал бы волну ложных «деплоев» на ровном месте.
SNAPSHOT_TTL_SECONDS = 7 * 24 * 3600


def _redis():
    """Клиент Redis или None — тот же, что у heartbeat-ключей."""
    try:
        from app.services.digest.state import _get_beat_redis
        return _get_beat_redis()
    except Exception as e:  # noqa: BLE001 — без Redis задача просто no-op
        log.warning("deploy_watch.redis_unavailable", error=str(e))
        return None


def _workload_key(ns: str, kind: str, name: str) -> str:
    return f"{ns}/{kind}/{name}"


def _template_hash(spec: Dict[str, Any]) -> Optional[str]:
    """sha256 от `spec.template` — того, что описывает под.

    Сюда входят образы, env, аргументы, ресурсы, тома, аннотации шаблона.
    НЕ входит `spec.replicas` — он лежит уровнем выше, и это единственное,
    что отличает «выкатили» от «отскейлили». `sort_keys` — чтобы порядок
    ключей в ответе API не считался изменением.
    """
    template = spec.get("template")
    if not isinstance(template, dict):
        return None
    payload = json.dumps(template, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _current_state(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """(uid, template_hash, images) из объекта kubectl или None если не разобрать.

    `generation` сохраняется только как справка для записи — критерием
    выката он не является (см. докстринг модуля).
    """
    meta = obj.get("metadata") or {}
    name = meta.get("name")
    if not name:
        return None
    spec = obj.get("spec") or {}
    containers = ((spec.get("template") or {}).get("spec") or {}).get("containers") or []
    return {
        "ns": meta.get("namespace") or "default",
        "name": name,
        "kind": obj.get("kind") or "Deployment",
        "uid": meta.get("uid"),
        "generation": meta.get("generation"),
        "template_hash": _template_hash(spec),
        "images": sorted(c.get("image") for c in containers if c.get("image")),
    }


def _rollout_reason(prev: Dict[str, Any], cur: Dict[str, Any]) -> Optional[str]:
    """Что изменилось: 'image' | 'template' | None.

    `image` — сменился хотя бы один образ: это выкат или откат кода.
    `template` — образы те же, но изменился `spec.template` (env, args,
    ресурсы, тома): конфигурационный выкат, поды пересоздаются.
    Рост `generation` без изменения шаблона (scale, HPA) — не деплой.

    Снимок без `template_hash` (записан версией до этого критерия) — с ним
    сравнивать нечего: по образам да, по шаблону — нет. Иначе первый прогон
    после обновления объявил бы «template» всему кластеру.

    Смена `uid` — не rollout, а пересоздание объекта: у нового workload'а
    история предыдущего к нему не относится. Такой случай отдаётся отдельно
    (см. вызывающий), чтобы «пересоздали стенд» не выглядело как «выкатили».
    """
    if prev.get("images") != cur.get("images"):
        return "image"
    prev_h, cur_h = prev.get("template_hash"), cur.get("template_hash")
    if prev_h and cur_h and prev_h != cur_h:
        return "template"
    return None


def _target_service(
    db: Session, ns: str, name: str,
) -> Optional[Service]:
    """Узел, на который вешать запись.

    Сначала `service`-узел: именно его читает `recent_deploys_for`, и запись
    на workload-узел до потребителя не дошла бы. Если такого нет — workload,
    потому что потерять событие хуже, чем записать его на соседний узел.
    """
    svc = (
        db.query(Service)
        .filter(
            Service.namespace == ns,
            Service.name == name,
            Service.node_kind == NODE_KIND_SERVICE,
        )
        .one_or_none()
    )
    if svc is not None:
        return svc
    return (
        db.query(Service)
        .filter(
            Service.namespace == ns,
            Service.name == name,
            Service.node_kind == NODE_KIND_WORKLOAD,
        )
        .one_or_none()
    )


def _read_snapshot(client) -> Dict[str, Dict[str, Any]]:
    try:
        raw = client.hgetall(SNAPSHOT_KEY) or {}
    except Exception as e:  # noqa: BLE001
        log.warning("deploy_watch.snapshot_read_failed", error=str(e))
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, value in raw.items():
        if isinstance(key, bytes):
            key = key.decode("utf-8", "replace")
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            out[key] = parsed
    return out


def _write_snapshot(client, states: Dict[str, Dict[str, Any]]) -> None:
    if not states:
        return
    try:
        client.delete(SNAPSHOT_KEY)
        client.hset(SNAPSHOT_KEY, mapping={
            k: json.dumps(v) for k, v in states.items()
        })
        client.expire(SNAPSHOT_KEY, SNAPSHOT_TTL_SECONDS)
    except Exception as e:  # noqa: BLE001
        log.warning("deploy_watch.snapshot_write_failed", error=str(e))


def watch_k8s_rollouts(
    db: Session,
    workloads: Optional[List[Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Сравнить состояние workload'ов с прошлым прогоном и записать выкаты.

    `workloads` — список объектов kubectl; по умолчанию берётся из кластера.
    Параметр нужен тестам и тому, кто уже сходил за этим списком.
    """
    stamp = now or datetime.utcnow()
    client = _redis()
    if client is None:
        return {"skipped": "redis_unavailable", "recorded": 0}

    if workloads is None:
        from app.knowledge_graph.k8s_topology_resources_sync import \
            _kubectl_get_deployments_all
        workloads = _kubectl_get_deployments_all()

    states: Dict[str, Dict[str, Any]] = {}
    for obj in workloads or []:
        state = _current_state(obj)
        if state is None:
            continue
        states[_workload_key(state["ns"], state["kind"], state["name"])] = state

    if not states:
        # Пустой ответ kubectl — это сбой запроса, а не опустевший кластер.
        # Затирать снимок им нельзя: следующий прогон объявил бы выкатом
        # каждый workload кластера.
        log.warning("deploy_watch.empty_cluster_response")
        return {"skipped": "empty_response", "recorded": 0}

    previous = _read_snapshot(client)
    stats: Dict[str, Any] = {
        "workloads": len(states),
        "recorded": 0,
        "by_reason": {"image": 0, "template": 0},
        "reincarnated": 0,
        "no_node": 0,
        "first_run": not previous,
    }

    if not previous:
        # Сравнивать не с чем: запоминаем и выходим. Записать «деплой» для
        # каждого из 4360 workload'ов означало бы датировать всю
        # инфраструктуру моментом первого запуска задачи.
        _write_snapshot(client, states)
        log.info("deploy_watch.first_run workloads=%d", len(states))
        return stats

    for key, cur in states.items():
        prev = previous.get(key)
        if prev is None:
            # Новый workload. Это не выкат новой версии существующего
            # сервиса, и записывать его как деплой — та же ложь, что и на
            # первом прогоне, только в розницу.
            continue
        if prev.get("uid") and cur.get("uid") and prev["uid"] != cur["uid"]:
            # Объект пересоздан: generation у нового снова 1, история
            # предыдущего к нему отношения не имеет.
            stats["reincarnated"] += 1
            continue
        reason = _rollout_reason(prev, cur)
        if reason is None:
            continue

        node = _target_service(db, cur["ns"], cur["name"])
        if node is None:
            stats["no_node"] += 1
            continue

        try:
            record_deployment(
                db,
                service=node,
                started_at=stamp,
                finished_at=stamp,
                status="SUCCESS",
                buildtype_id=BUILDTYPE_ID,
                # Ключ дедупа — uid + хэш шаблона, а не generation: у
                # автоскейлящегося workload'а generation даёт новый номер на
                # каждый тик HPA, и одна и та же версия писалась бы снова.
                build_number=f"{cur.get('uid')}:{cur.get('template_hash')}",
                extras={
                    "attribution": ATTRIBUTION,
                    # Наблюдение за КОНКРЕТНЫМ объектом — не ns-broadcast.
                    # Именно эта запись даёт `stale_classifier` право
                    # назвать сервис `active`.
                    "namespace_scope": False,
                    "rollout_reason": reason,
                    "workload_kind": cur.get("kind"),
                    "workload_uid": cur.get("uid"),
                    # Справочно — критерием не является (см. докстринг модуля).
                    "generation": cur.get("generation"),
                    "template_hash": cur.get("template_hash"),
                    "previous_template_hash": prev.get("template_hash"),
                    "images": cur.get("images"),
                    "previous_images": prev.get("images"),
                },
            )
        except Exception as e:  # noqa: BLE001 — один сервис не валит прогон
            log.warning(
                "deploy_watch.record_failed",
                namespace=cur["ns"], name=cur["name"], error=str(e),
            )
            continue
        stats["recorded"] += 1
        stats["by_reason"][reason] += 1

    if stats["recorded"]:
        db.commit()
    _write_snapshot(client, states)
    log.info("deploy_watch.done", **{
        k: v for k, v in stats.items() if k != "by_reason"
    })
    return stats
