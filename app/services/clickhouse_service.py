"""ClickHouse blast-radius enrichment.

Запрашивает UserActivityMinuteFact вокруг starts_at инцидента и возвращает
число активных игроков до/во время краша — это сигнал blast radius.

Namespace → CH environment mapping:
  prod-*      → prod
  preprod-*   → preprod
  preupdate-* → preupdate
  squad-N-*   → squad-N
  иначе       → prod (safe fallback)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_NS_TO_ENV = {
    "prod": "prod",
    "preprod": "preprod",
    "preupdate": "preupdate",
}


def _ns_to_ch_env(namespace: str) -> str:
    for prefix, env in _NS_TO_ENV.items():
        if namespace.startswith(prefix):
            return env
    # squad-1-*, squad-2-* etc
    if namespace.startswith("squad-"):
        parts = namespace.split("-")
        if len(parts) >= 2:
            return f"squad-{parts[1]}"
    return "prod"


def _parse_ts(raw: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(raw.replace("Z", "+0000"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


class ClickHouseClient:
    def __init__(self, host: str, port: int, user: str, password: str, timeout: float = 8.0):
        self._base = f"http://{host}:{port}"
        self._auth = (user, password)
        self._timeout = timeout

    async def query(self, sql: str) -> List[Dict[str, Any]]:
        """Выполнить SELECT, вернуть список dicts (column→value)."""
        params = {
            "database": "WOAnalytics",
            "default_format": "JSONCompact",
        }
        body = sql.strip() + "\nFORMAT JSONCompact"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(
                self._base + "/",
                content=body.encode(),
                params=params,
                auth=self._auth,
                headers={"Content-Type": "text/plain; charset=utf-8"},
            )
            r.raise_for_status()
            data = r.json()
        cols = [m["name"] for m in data["meta"]]
        return [dict(zip(cols, row)) for row in data["data"]]


async def get_blast_radius(
    namespace: str,
    starts_at: str,
    window_minutes: Optional[int] = None,
) -> Optional[str]:
    """Вернуть строку blast-radius для инжекта в LLM-промпт.

    Запрашивает UserActivityMinuteFact для окружения namespace.
    Сравнивает:
      - baseline: среднее за 10 минут ДО incident window
      - incident window: среднее за window_minutes минут вокруг starts_at
    """
    if not settings.CH_PROD_HOST or not settings.CH_PROD_PASSWORD:
        return None

    win = window_minutes or settings.CH_BLAST_RADIUS_WINDOW_MINUTES
    ts = _parse_ts(starts_at)
    if ts is None:
        return None

    env = _ns_to_ch_env(namespace)
    half = timedelta(minutes=win // 2)
    baseline_end = ts - half
    baseline_start = baseline_end - timedelta(minutes=10)
    window_start = ts - half
    window_end = ts + half

    def fmt(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    sql = f"""
SELECT
    Minute,
    countDistinct(DynUserId) AS active_users
FROM UserActivityMinuteFact
WHERE Minute >= toDateTime('{fmt(baseline_start)}')
  AND Minute <= toDateTime('{fmt(window_end)}')
GROUP BY Minute
ORDER BY Minute
"""

    client = ClickHouseClient(
        settings.CH_PROD_HOST,
        settings.CH_PROD_PORT,
        settings.CH_PROD_USER,
        settings.CH_PROD_PASSWORD,
    )
    try:
        rows = await client.query(sql)
    except Exception as e:
        logger.warning("clickhouse.blast_radius_failed ns=%s: %s", namespace, e)
        return None

    if not rows:
        return None

    baseline_rows = [r for r in rows if fmt(baseline_start) <= str(r["Minute"]) < fmt(window_start)]
    incident_rows = [r for r in rows if fmt(window_start) <= str(r["Minute"]) <= fmt(window_end)]

    if not incident_rows:
        return None

    def avg_users(rs: List[Dict]) -> float:
        if not rs:
            return 0.0
        return sum(int(r["active_users"]) for r in rs) / len(rs)

    baseline_avg = avg_users(baseline_rows)
    incident_avg = avg_users(incident_rows)
    incident_min = min(int(r["active_users"]) for r in incident_rows)
    incident_max = max(int(r["active_users"]) for r in incident_rows)

    drop_pct = 0
    if baseline_avg > 0:
        drop_pct = round((baseline_avg - incident_avg) / baseline_avg * 100)

    lines = [f"=== BLAST RADIUS ({env} env) ==="]
    lines.append(f"Active players baseline (before incident): ~{baseline_avg:.0f}/min")
    lines.append(
        f"Active players during incident window: avg={incident_avg:.0f}/min "
        f"(min={incident_min}, max={incident_max})"
    )
    if drop_pct > 5:
        lines.append(f"Player activity drop: {drop_pct}% vs baseline")
    elif drop_pct < -5:
        lines.append(f"Player activity INCREASED {abs(drop_pct)}% (isolated pod issue, not cluster-wide)")
    else:
        lines.append("Player activity: stable (isolated pod, no visible blast radius)")

    return "\n".join(lines)
