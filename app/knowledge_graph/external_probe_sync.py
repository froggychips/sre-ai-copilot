"""External endpoint health probe → KG → Discord alerts.

Источник endpoints: synthetic services `ingress:<host>` (team_owner='external'),
которые создаёт `kg_ingress_sync.py` из k8s Ingress hosts.

Цикл (вызывается из beat task `kg_external_probe` каждые EXTERNAL_PROBE_INTERVAL):
  1. DNS resolve hostname через `socket.getaddrinfo` — multi-A-record support
  2. На каждый IP параллельно: TCP/:port connect + один HTTPS HEAD на hostname
  3. Aggregate: `up` (все IP TCP-ok + HTTP<500) | `degraded` (часть IP fail) | `down` (все IP fail)
  4. State + результаты пишутся в `Service.metadata_json.external_probe` — без новой таблицы
  5. При consecutive_failures >= FAIL_THRESHOLD и не-firing → AlertEvent + Discord embed
  6. При возврате в `up` и firing → resolve AlertEvent (resolved_at) + Discord resolved-embed

Мотивация per-IP probe: grafana.lastoasisgame.com 2026-05-19 имел 2 A-records,
один таймаутил (DiskPressure на ноде), второй работал — итог: `degraded`,
а не полный `down`. См. инцидент `project_dev1_diskpressure_grafana_outage`.

CLI:
    python -m app.knowledge_graph.external_probe_sync
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from datetime import datetime
from typing import Any, Dict, List, cast

import httpx
import structlog
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.knowledge_graph.populator import record_alert_event
from app.knowledge_graph.schema import AlertEvent, Service

log = structlog.get_logger()


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
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
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
    повторным запускам в любой момент."""
    if not settings.EXTERNAL_PROBE_ENABLED:
        log.info("external_probe.disabled")
        return {"skipped": "disabled"}

    from app.services.discord_service import DiscordService
    discord = DiscordService()

    timeout = float(settings.EXTERNAL_PROBE_TIMEOUT_SECONDS)
    threshold = int(settings.EXTERNAL_PROBE_FAIL_THRESHOLD)

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

    stats: Dict[str, Any] = {
        "probed": 0,
        "up": 0,
        "degraded": 0,
        "down": 0,
        "alerts_fired": 0,
        "alerts_resolved": 0,
        "skipped_wildcard": 0,
    }

    for svc in services:
        host = svc.name.split(":", 1)[1] if ":" in svc.name else ""
        if not host or host == "*":
            stats["skipped_wildcard"] += 1
            continue
        stats["probed"] += 1

        snapshot = await _probe_endpoint(host, timeout)
        status = snapshot["status"]
        stats[status] += 1

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
            await _send_alert(discord, host, status, snapshot, resolved=False)
            record_alert_event(
                db=db,
                service=svc,
                alertname=f"ExternalProbe{status.capitalize()}",
                severity="critical" if status == "down" else "warning",
                fingerprint=f"external_probe:{host}",
                fired_at=datetime.utcnow(),
                raw={"host": host, "status": status, "snapshot": snapshot},
            )
            stats["alerts_fired"] += 1
        elif should_resolve:
            snapshot["firing"] = False
            snapshot["last_resolved_at"] = snapshot["last_at"]
            await _send_alert(discord, host, "up", snapshot, resolved=True)
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
            stats["alerts_resolved"] += 1

        meta["external_probe"] = snapshot
        svc.metadata_json = cast(Any, meta)
        flag_modified(svc, "metadata_json")

    db.commit()
    log.info(
        "external_probe.done",
        probed=stats["probed"], up=stats["up"], degraded=stats["degraded"],
        down=stats["down"], fired=stats["alerts_fired"], resolved=stats["alerts_resolved"],
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
