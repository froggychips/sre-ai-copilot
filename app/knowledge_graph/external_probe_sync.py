"""External endpoint health probe → KG → Discord alerts.

Источник endpoints: synthetic services `ingress:<host>` (team_owner='external'),
которые создаёт `kg_ingress_sync.py` из k8s Ingress hosts.

Цикл (вызывается из beat task `kg_external_probe` каждые EXTERNAL_PROBE_INTERVAL)
разбит на фазы по дисциплине «читать → закрыть транзакцию → probe без tx →
писать коротко» — класс инцидента 08–10.08.2026 idle-in-transaction (см.
docstring run_external_probe):
  1. snapshot: SELECT целей → plain-структуры, db.commit() закрывает read-tx
  2. probe: на каждый hostname DNS resolve (`socket.getaddrinfo`,
     multi-A-record) + на каждый IP параллельно TCP/:443 connect + один
     HTTPS HEAD на hostname; aggregate: `up` (все IP TCP-ok + HTTP<500) |
     `degraded` (часть IP fail) | `down` (все IP fail). Ни одного SQL.
  3. write: перечитать строки по id; state + результаты в
     `Service.metadata_json.external_probe` (без новой таблицы); при
     consecutive_failures >= FAIL_THRESHOLD и не-firing → AlertEvent; при
     возврате в `up` и firing → resolve AlertEvent (resolved_at); commit
  4. notify: Discord embeds строго ПОСЛЕ commit — алерт только про
     зафиксированный state

Мотивация per-IP probe: grafana.lastoasisgame.com 2026-05-19 имел 2 A-records,
один таймаутил (DiskPressure на ноде), второй работал — итог: `degraded`,
а не полный `down`. См. инцидент `project_dev1_diskpressure_grafana_outage`.

CLI:
    python -m app.knowledge_graph.external_probe_sync
"""
from __future__ import annotations

import asyncio
import logging
import re
import socket
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, cast

import httpx
import structlog
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.knowledge_graph.populator import record_alert_event
from app.knowledge_graph.schema import AlertEvent, Service

log = structlog.get_logger()


# ── Лок от одновременных прогонов ────────────────────────────────────────────
# Beat тикает каждую минуту, а последовательный probe-цикл при N хостах ×
# таймаут 5с легко живёт дольше 60с. `options.expires=50` в beat-расписании
# дропает только НЕПОДХВАЧЕННЫЕ тики из очереди — два уже начавшихся прогона
# он не исключает, а параллельные прогоны гоняют read-modify-write по
# metadata_json наперегонки: потерянные consecutive_failures, двойные
# fire/resolve. Готового лок-хелпера в репо нет; берём redis SET NX EX —
# redis уже в стеке (celery broker + LoopLocalRedis), а session-level
# pg_try_advisory_lock не дружит с нашей же дисциплиной коммитов: после
# db.commit() соединение уходит в пул вместе с невозвращённым локом.
_LOCK_KEY = "kg:external_probe:running"
# Потолок жизни лока — страховка от прогона, убитого мимо finally
# (OOM/SIGKILL воркера): максимум ~5 минут пропущенных тиков, не вечный стоп.
_LOCK_TTL_SECONDS = 300


def _get_redis():
    """Ленивый импорт: app.celery_worker тянет весь app при импорте —
    модулю probe он нужен только ради готового LoopLocalRedis-клиента."""
    from app.celery_worker import redis_client
    return redis_client


async def _try_acquire_lock() -> Optional[str]:
    """Захватить лок прогона. Возвращает:
      * hex-токен — лок наш, отпустить через `_release_lock`;
      * None      — лок держит другой прогон, этот тик скипаем;
      * ""        — redis недоступен: fail-open, работаем без лока
                    (живой probe важнее защиты от наложения).
    """
    token = uuid.uuid4().hex
    try:
        ok = await _get_redis().set(_LOCK_KEY, token, nx=True, ex=_LOCK_TTL_SECONDS)
    except Exception as e:
        log.warning("external_probe.lock_unavailable", error=str(e))
        return ""
    return token if ok else None


async def _release_lock(token: str) -> None:
    """Отпустить лок, только если он всё ещё наш: прогон длиннее TTL терял
    лок — удалять перезахваченный чужой нельзя. token='' = работали без лока."""
    if not token:
        return
    try:
        r = _get_redis()
        current = await r.get(_LOCK_KEY)
        if isinstance(current, bytes):
            current = current.decode()
        if current == token:
            await r.delete(_LOCK_KEY)
    except Exception as e:
        log.warning("external_probe.lock_release_failed", error=str(e))


# ── Фильтр целей (M2 ревью) ──────────────────────────────────────────────────
# Под селектор probe (synthetic + team_owner='external' + name LIKE 'ingress:%')
# попадают ДВА вида узлов: `ingress:<host>` из kg_ingress_sync (наши цели) и
# `ingress:<resource-name>` из sync_topology_resources — у тех хвост это имя
# k8s-Ingress-РЕСУРСА, не hostname. Пробовать их = гарантированный DNS-fail и
# ложный ExternalProbeDown в Discord. `discovered_by` у узлов нет (это поле
# рёбер), поэтому различаем по двум признакам:
#   * topology-узлы несут metadata_json['k8s_ingress'] (см.
#     k8s_topology_resources_sync._sync_one_ingress) — явный маркер источника;
#   * хвост имени обязан быть RFC-1123 hostname С ТОЧКОЙ (FQDN) — имя ресурса
#     вида 'grafana-ingress' отсекается уже формой.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
    re.IGNORECASE,
)


async def _probe_ip_tcp(ip: str, port: int, timeout: float) -> Dict[str, Any]:
    """TCP-connect к (ip, port) с таймаутом. Возвращает {ip, tcp_ok, latency_ms, error}."""
    start = time.monotonic()
    result: Dict[str, Any] = {"ip": ip, "port": port, "tcp_ok": False, "latency_ms": None, "error": None}
    loop = asyncio.get_event_loop()
    try:
        def _connect():
            s = socket.create_connection((ip, port), timeout=timeout)
            s.close()
        await asyncio.wait_for(loop.run_in_executor(None, _connect), timeout=timeout + 1)
        result["tcp_ok"] = True
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    result["latency_ms"] = int((time.monotonic() - start) * 1000)
    return result


async def _probe_https_head(host: str, timeout: float) -> Dict[str, Any]:
    """HTTPS HEAD по hostname (через системный DNS). verify=False — cert mismatch
    при wildcard/letsencrypt не должен валить probe; мы проверяем доступность,
    не security. Возвращает {http_code, latency_ms, error}.
    """
    start = time.monotonic()
    result: Dict[str, Any] = {"http_code": None, "latency_ms": None, "error": None}
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:  # nosec B501 — external probe ходит на любые hosts включая self-signed cert
            r = await client.head(f"https://{host}/", follow_redirects=False)
            result["http_code"] = r.status_code
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    result["latency_ms"] = int((time.monotonic() - start) * 1000)
    return result


def _aggregate_status(ips: List[str], tcp_results: List[Dict[str, Any]], http_result: Dict[str, Any]) -> str:
    """up | degraded | down.

    `up`       — есть DNS-resolve, все IP TCP-ok, HTTPS HEAD ответил <500 (или 0 — означает HTTP не пробовали, см. ниже).
    `degraded` — часть IP TCP fail, либо HTTPS HEAD вернул 5xx
    `down`     — DNS пуст, все IP TCP fail, либо HTTPS HEAD пробросил error.
    """
    if not ips:
        return "down"
    tcp_ok_count = sum(1 for r in tcp_results if r.get("tcp_ok"))
    if tcp_ok_count == 0:
        return "down"
    http_code = http_result.get("http_code") or 0
    http_err = http_result.get("error")
    if tcp_ok_count < len(ips):
        return "degraded"
    if http_err or http_code >= 500:
        return "degraded"
    return "up"


async def _probe_endpoint(host: str, timeout: float) -> Dict[str, Any]:
    """DNS+TCP+HTTPS для одного hostname. Возвращает полный snapshot:
    `{host, last_at, status, ips, tcp_results, http_result, dns_error}`."""
    snapshot: Dict[str, Any] = {
        "host": host,
        "last_at": datetime.utcnow().isoformat() + "Z",
        "ips": [],
        "tcp_results": [],
        "http_result": {},
        "dns_error": None,
        "status": "down",
    }
    try:
        infos = socket.getaddrinfo(host, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)
        snapshot["ips"] = sorted({i[4][0] for i in infos})
    except Exception as e:
        snapshot["dns_error"] = f"{type(e).__name__}: {str(e)[:120]}"

    if snapshot["ips"]:
        tcp_results, http_result = await asyncio.gather(
            asyncio.gather(*[_probe_ip_tcp(ip, 443, timeout) for ip in snapshot["ips"]]),
            _probe_https_head(host, timeout),
        )
        snapshot["tcp_results"] = list(tcp_results)
        snapshot["http_result"] = http_result

    snapshot["status"] = _aggregate_status(snapshot["ips"], snapshot["tcp_results"], snapshot["http_result"])
    return snapshot


async def _send_alert(discord, host: str, status: str, snapshot: Dict[str, Any], resolved: bool) -> None:
    """Wrapper для discord.send_external_probe_alert с защитой от падения."""
    try:
        await discord.send_external_probe_alert(host=host, status=status, snapshot=snapshot, resolved=resolved)
    except Exception as e:
        log.warning("external_probe.discord_send_failed", host=host, error=str(e))


async def run_external_probe(db: Session) -> Dict[str, Any]:
    """Главный entry-point. Идемпотентен (state в metadata_json), безопасен к
    повторным запускам; одновременные прогоны отсекает redis-лок.

    Дисциплина транзакций — «читать → закрыть → probe без tx → писать
    коротко». Класс инцидента 08–10.08.2026 (idle-in-transaction): SELECT
    целей открывал транзакцию, дальше весь probe-цикл (5с × N хостов) шёл
    без единого SQL — с ~25+ хостов соединение гарантированно убивал
    idle_in_transaction_session_timeout=120s (app/database.py). commit падал,
    state (consecutive_failures/firing) и AlertEvent откатывались, а
    Discord-алерты уже улетели → каждый следующий тик слал те же алерты
    заново и не мог их зарезолвить.
    """
    if not settings.EXTERNAL_PROBE_ENABLED:
        log.info("external_probe.disabled")
        return {"skipped": "disabled"}

    lock_token = await _try_acquire_lock()
    if lock_token is None:
        log.info("external_probe.already_running")
        return {"skipped": "already_running"}
    try:
        return await _run_probe_cycle(db)
    finally:
        await _release_lock(lock_token)


async def _run_probe_cycle(db: Session) -> Dict[str, Any]:
    """Тело прогона (под локом). Фазы: snapshot → probe → write → notify."""
    from app.services.discord_service import DiscordService
    discord = DiscordService()

    timeout = float(settings.EXTERNAL_PROBE_TIMEOUT_SECONDS)
    threshold = int(settings.EXTERNAL_PROBE_FAIL_THRESHOLD)

    stats: Dict[str, Any] = {
        "probed": 0,
        "up": 0,
        "degraded": 0,
        "down": 0,
        "alerts_fired": 0,
        "alerts_resolved": 0,
        "skipped_wildcard": 0,
        "skipped_non_hostname": 0,
    }

    # ── Фаза 1: snapshot целей в plain-структуры ────────────────────────────
    # Берём synthetic external-узлы. Wildcard `*` и default-backend пропускаем —
    # это catch-all, не реальный hostname.
    services = (
        db.query(Service)
        .filter(
            Service.synthetic == True,  # noqa: E712 (SQLAlchemy
            Service.team_owner == "external",
            Service.name.like("ingress:%"),
        )
        .all()
    )
    targets: List[Tuple[int, str]] = []  # (service_id, host) — без ORM-ссылок
    for svc in services:
        host = svc.name.split(":", 1)[1] if ":" in svc.name else ""
        if not host or host == "*":
            stats["skipped_wildcard"] += 1
            continue
        # M2: узел `ingress:<resource-name>` от sync_topology_resources — не цель.
        if "k8s_ingress" in (svc.metadata_json or {}):
            stats["skipped_non_hostname"] += 1
            log.info(
                "external_probe.skip_ingress_resource_node",
                name=svc.name, namespace=svc.namespace,
            )
            continue
        if not _HOSTNAME_RE.match(host):
            stats["skipped_non_hostname"] += 1
            log.info("external_probe.skip_invalid_hostname", name=svc.name, host=host)
            continue
        targets.append((cast(int, svc.id), host))
    # Закрыть read-транзакцию ДО probe-цикла: иначе она висит idle все
    # 5с × N хостов и упирается в idle_in_transaction_session_timeout.
    db.commit()

    # ── Фаза 2: probe — ни одного SQL ────────────────────────────────────────
    probe_results: List[Tuple[int, str, Dict[str, Any]]] = []
    for svc_id, host in targets:
        stats["probed"] += 1
        snapshot = await _probe_endpoint(host, timeout)
        stats[snapshot["status"]] += 1
        probe_results.append((svc_id, host, snapshot))

    # ── Фаза 3: короткая write-фаза ──────────────────────────────────────────
    # Строки перечитываем по id: за время probe узел могли удалить (drift
    # cleanup) или обновить — снапшоту фазы 1 не доверяем.
    notifications: List[Dict[str, Any]] = []
    if probe_results:
        rows: Dict[int, Service] = {
            cast(int, s.id): s
            for s in (
                db.query(Service)
                .filter(Service.id.in_([sid for sid, _, _ in probe_results]))
                .all()
            )
        }
        for svc_id, host, snapshot in probe_results:
            svc = rows.get(svc_id)
            if svc is None:
                continue  # узел удалён между фазами
            status = snapshot["status"]

            # State machine — читаем prev из metadata, обновляем
            meta = dict(svc.metadata_json or {})
            prev = meta.get("external_probe") or {}
            prev_failures = int(prev.get("consecutive_failures") or 0)
            prev_firing = bool(prev.get("firing") or False)

            if status == "up":
                snapshot["consecutive_failures"] = 0
            else:
                snapshot["consecutive_failures"] = prev_failures + 1

            should_fire = (status != "up") and snapshot["consecutive_failures"] >= threshold and not prev_firing
            should_resolve = (status == "up") and prev_firing

            snapshot["firing"] = prev_firing
            if should_fire:
                snapshot["firing"] = True
                snapshot["last_alert_at"] = snapshot["last_at"]
                record_alert_event(
                    db=db,
                    service=svc,
                    alertname=f"ExternalProbe{status.capitalize()}",
                    severity="critical" if status == "down" else "warning",
                    fingerprint=f"external_probe:{host}",
                    fired_at=datetime.utcnow(),
                    raw={"host": host, "status": status, "snapshot": snapshot},
                )
                notifications.append(
                    {"host": host, "status": status, "snapshot": snapshot, "resolved": False},
                )
                stats["alerts_fired"] += 1
            elif should_resolve:
                snapshot["firing"] = False
                snapshot["last_resolved_at"] = snapshot["last_at"]
                ae = (
                    db.query(AlertEvent)
                    .filter(
                        AlertEvent.fingerprint == f"external_probe:{host}",
                        AlertEvent.resolved_at.is_(None),
                    )
                    .one_or_none()
                )
                if ae is not None:
                    ae.resolved_at = cast(Any, datetime.utcnow())
                notifications.append(
                    {"host": host, "status": "up", "snapshot": snapshot, "resolved": True},
                )
                stats["alerts_resolved"] += 1

            meta["external_probe"] = snapshot
            svc.metadata_json = cast(Any, meta)
            flag_modified(svc, "metadata_json")

        db.commit()

    # ── Фаза 4: Discord строго ПОСЛЕ commit ─────────────────────────────────
    # Раньше send шёл до commit: при упавшем commit state откатывался, а алерт
    # уже улетел — следующий тик слал тот же embed заново и не мог его
    # зарезолвить (источник дублей 08–10.08.2026). Обратная цена осознанна:
    # упавший send после commit не повторится (_send_alert только логирует),
    # но resolve при восстановлении хоста всё равно уйдёт.
    for n in notifications:
        await _send_alert(discord, n["host"], n["status"], n["snapshot"], resolved=n["resolved"])

    log.info(
        "external_probe.done",
        probed=stats["probed"], up=stats["up"], degraded=stats["degraded"],
        down=stats["down"], fired=stats["alerts_fired"], resolved=stats["alerts_resolved"],
        skipped_non_hostname=stats["skipped_non_hostname"],
    )
    return stats


if __name__ == "__main__":
    from app.database import SessionLocal
    logging.basicConfig(level=logging.INFO)
    db = SessionLocal()
    try:
        result = asyncio.run(run_external_probe(db))
        print(result)
    finally:
        db.close()
