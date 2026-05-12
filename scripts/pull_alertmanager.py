"""Pull-mode adapter: AlertManager v2 API → local /webhooks/alertmanager.

Полностью stdlib. Раз в POLL_INTERVAL_SECONDS дёргает active-alerts из
AlertManager v2 API, преобразует их в AlertManagerWebhook-payload и POST-ит
в локальный sre-ai-copilot. Дедупликация по (fingerprint, status, updatedAt).

ENV:
    ALERTMANAGER_URL          default http://localhost:9093
    SRE_COPILOT_URL           default http://localhost:8000
    POLL_INTERVAL_SECONDS     default 30
    INCLUDE_INHIBITED         default false
    INCLUDE_SILENCED          default false
    STATE_FILE                default ~/.cache/sre-ai-copilot/pulled.json
    DRY_RUN                   default false  — печатать payload, не POST-ить
    FILTER                    optional repeatable alertmanager filter (e.g. "severity=critical")

Пример (требует `kubectl port-forward -n monitoring svc/vmalertmanager-vm-victoria-metrics-k8s-stack 9093:9093`):

    python scripts/pull_alertmanager.py --once --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger("pull-alertmanager")

DEFAULT_STATE_FILE = pathlib.Path.home() / ".cache" / "sre-ai-copilot" / "pulled.json"


def _env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in (
        "1",
        "true",
        "yes",
        "y",
    )


def fetch_alerts(
    am_url: str, filters: list[str], include_inhibited: bool, include_silenced: bool
) -> list[dict[str, Any]]:
    qs = {
        "active": "true",
        "silenced": "true" if include_silenced else "false",
        "inhibited": "true" if include_inhibited else "false",
    }
    url = f"{am_url.rstrip('/')}/api/v2/alerts?" + urllib.parse.urlencode(qs)
    for f in filters:
        url += "&" + urllib.parse.urlencode({"filter": f})
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def to_webhook_payload(
    alerts: list[dict[str, Any]], external_url: str
) -> dict[str, Any]:
    """Сборка batch-payload в формате AlertManagerWebhook (v4)."""
    return {
        "version": "4",
        "groupKey": "pull-mode/active",
        "status": "firing",
        "receiver": "sre-ai-copilot-pull",
        "groupLabels": {},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": external_url,
        "alerts": [
            {
                "status": a.get("status", {}).get("state", "active"),
                "labels": a.get("labels", {}),
                "annotations": a.get("annotations", {}),
                "startsAt": a.get("startsAt", ""),
                "endsAt": a.get("endsAt"),
                "generatorURL": a.get("generatorURL"),
                "fingerprint": a.get("fingerprint", ""),
            }
            for a in alerts
        ],
    }


def normalise_status(a: dict[str, Any]) -> str:
    """AM v2 state ∈ {active, suppressed, unprocessed}. Преобразуем в webhook-v4 {firing,resolved}."""
    state = a.get("status", {}).get("state", "active")
    if state == "active":
        return "firing"
    if state == "suppressed":
        return "resolved"  # подавленный — для нас не «firing»
    return state


def dedup_key(a: dict[str, Any]) -> str:
    """fingerprint + state + updatedAt — алерт считается новым, если состояние или таймстамп поменялись."""
    return f"{a.get('fingerprint','')}::{a.get('status',{}).get('state','')}::{a.get('updatedAt','')}"


def load_state(path: pathlib.Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text()).get("seen", []))
    except Exception as e:
        log.warning("state file unreadable, starting fresh: %s", e)
        return set()


def save_state(path: pathlib.Path, seen: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"seen": sorted(seen)[-2000:]}, indent=2))


def post_webhook(copilot_url: str, payload: dict[str, Any]) -> tuple[int, str]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{copilot_url.rstrip('/')}/webhooks/alertmanager",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, r.read().decode(errors="replace")[:500]


def tick(args, state_file: pathlib.Path, seen: set[str]) -> int:
    """Один цикл pull. Возвращает кол-во новых алертов, переданных в copilot."""
    try:
        raw = fetch_alerts(
            args.alertmanager_url,
            args.filter,
            args.include_inhibited,
            args.include_silenced,
        )
    except urllib.error.URLError as e:
        log.error("AM fetch failed: %s", e)
        return 0
    new = [a for a in raw if dedup_key(a) not in seen]
    if not new:
        log.info("no new alerts (total active=%d)", len(raw))
        return 0
    # переписать status в firing/resolved для каждого
    for a in new:
        a["status"] = {"state": normalise_status(a)}
    payload = to_webhook_payload(new, args.alertmanager_url)
    if args.dry_run:
        log.info(
            "DRY: %d new alerts:\n%s", len(new), json.dumps(payload, indent=2)[:2000]
        )
    else:
        try:
            code, body = post_webhook(args.copilot_url, payload)
            log.info("posted %d alerts → HTTP %s: %s", len(new), code, body[:200])
        except urllib.error.HTTPError as e:
            log.error("copilot %d: %s", e.code, e.read().decode(errors="replace")[:300])
            return 0
        except urllib.error.URLError as e:
            log.error("copilot unreachable: %s", e)
            return 0
    seen.update(dedup_key(a) for a in new)
    save_state(state_file, seen)
    return len(new)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AlertManager → local sre-ai-copilot pull adapter"
    )
    parser.add_argument(
        "--alertmanager-url",
        default=os.environ.get("ALERTMANAGER_URL", "http://localhost:9093"),
    )
    parser.add_argument(
        "--copilot-url",
        default=os.environ.get("SRE_COPILOT_URL", "http://localhost:8000"),
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("POLL_INTERVAL_SECONDS", "30")),
    )
    parser.add_argument(
        "--state-file",
        type=pathlib.Path,
        default=pathlib.Path(os.environ.get("STATE_FILE", DEFAULT_STATE_FILE)),
    )
    parser.add_argument(
        "--include-inhibited",
        action="store_true",
        default=_env_bool("INCLUDE_INHIBITED"),
    )
    parser.add_argument(
        "--include-silenced", action="store_true", default=_env_bool("INCLUDE_SILENCED")
    )
    parser.add_argument(
        "--filter",
        action="append",
        default=(
            os.environ.get("FILTER", "").split(",") if os.environ.get("FILTER") else []
        ),
    )
    parser.add_argument("--dry-run", action="store_true", default=_env_bool("DRY_RUN"))
    parser.add_argument("--once", action="store_true", help="Один pull и выход")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args.filter = [f for f in args.filter if f]

    seen = load_state(args.state_file)
    log.info(
        "start: am=%s copilot=%s interval=%ss dry=%s state=%s seen=%d",
        args.alertmanager_url,
        args.copilot_url,
        args.interval,
        args.dry_run,
        args.state_file,
        len(seen),
    )

    if args.once:
        tick(args, args.state_file, seen)
        return 0

    stop = False

    def _shutdown(_sig, _frame):
        nonlocal stop
        stop = True
        log.info("shutdown requested")

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    while not stop:
        tick(args, args.state_file, seen)
        # дробим sleep, чтобы быстрее реагировать на сигналы
        for _ in range(args.interval):
            if stop:
                break
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
