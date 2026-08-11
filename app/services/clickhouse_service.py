"""ClickHouse blast-radius enrichment.

Запрашивает UserActivityMinuteFact вокруг starts_at инцидента и возвращает
число активных игроков до/во время краша — это сигнал blast radius.

Namespace → CH environment mapping:
  prod-*      → prod
  preprod-*   → preprod
  preupdate-* → preupdate
  squad-N-*   → squad-N
  иначе       → None (окружение не игровое / неизвестно)

ВАЖНО (см. _PROD_ONLY_NOTE у get_blast_radius): цифры отдаём ТОЛЬКО для
prod-namespace. Источник один — CH_PROD_HOST/WOAnalytics, per-env среза в нём нет.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from app.services.resilience import with_external_retry

from app.config import settings

logger = logging.getLogger(__name__)

_NS_TO_ENV = {
    "prod": "prod",
    "preprod": "preprod",
    "preupdate": "preupdate",
}


def _ns_to_ch_env(namespace: str) -> Optional[str]:
    """k8s-namespace → CH environment. None — namespace не игровой/неизвестный.

    Прежний fallback возвращал 'prod' для ЛЮБОГО нераспознанного namespace
    (monitoring, sre-ai, kube-system) — и такой инцидент получал прод-цифры
    активных игроков как «свои». Теперь неизвестное окружение честно None.
    """
    for prefix, env in _NS_TO_ENV.items():
        if namespace.startswith(prefix):
            return env
    # squad-1-*, squad-2-* etc
    if namespace.startswith("squad-"):
        parts = namespace.split("-")
        if len(parts) >= 2:
            return f"squad-{parts[1]}"
    return None


# Дробная часть секунд: AM/Prometheus шлют startsAt в RFC3339 с миллисекундами
# ('2026-08-10T12:34:56.789Z'), Go-сериализация умеет и 9 знаков (наносекунды).
# datetime.fromisoformat ест максимум 6 — лишнее обрезаем.
_FRACTION_RE = re.compile(r"\.(\d+)")


def _parse_ts(raw: str) -> Optional[datetime]:
    """RFC3339/ISO-8601 → tz-aware datetime (naive считаем UTC).

    Прежняя реализация перебирала strptime-форматы БЕЗ %f, поэтому реальный
    Alertmanager-овский startsAt с миллисекундами не парсился вообще → None →
    blast radius молча выключался почти на каждом алерте. Плюс первый формат
    ('…%SZ') был мёртвой ветвью: `replace("Z", "+0000")` убирал Z до strptime.
    """
    if not raw:
        return None
    s = raw.strip()
    # fromisoformat не ест 'Z' до 3.11 — нормализуем к смещению.
    if s[-1:] in ("Z", "z"):
        s = s[:-1] + "+00:00"
    s = _FRACTION_RE.sub(lambda m: "." + m.group(1)[:6], s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# Порты, на которых CH слушает HTTPS (8443 — то что стоит в .env.example).
_CH_TLS_PORTS = {443, 8443}


def _ch_scheme(port: int) -> str:
    """Схема эндпоинта CH: настройка CH_PROD_SCHEME, 'auto' → по порту."""
    configured = (getattr(settings, "CH_PROD_SCHEME", "") or "auto").strip().lower()
    if configured in ("http", "https"):
        return configured
    return "https" if port in _CH_TLS_PORTS else "http"


class ClickHouseClient:
    def __init__(self, host: str, port: int, user: str, password: str, timeout: float = 8.0,
                 scheme: Optional[str] = None):
        # Схема больше не хардкод `http://`: с CH_PROD_PORT=8443 (см.
        # .env.example) plain-HTTP запрос упирался в TLS-хендшейк и blast
        # radius тихо отключался.
        self._base = f"{scheme or _ch_scheme(port)}://{host}:{port}"
        self._auth = (user, password)
        self._timeout = timeout

    @with_external_retry(
        max_attempts=3, initial_delay=0.5, name="clickhouse.query",
        # Только транспортные ошибки httpx (connect/read timeout, обрыв
        # коннекта). httpx.HTTPStatusError сюда НЕ входит: CH отвечает
        # 400/401/5xx и на битый SQL, и на неверные креды — это
        # детерминированные ошибки, их повтор 3× лишь жёг время.
        retry_on=(httpx.TransportError,),
    )
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

    ТОЛЬКО для prod-namespace (см. _PROD_ONLY_NOTE ниже). Для preprod/
    preupdate/squad-N и не-игровых namespace возвращает None — секция не
    отдаётся вовсе.

    Запрашивает UserActivityMinuteFact прод-аналитики. Сравнивает:
      - baseline: среднее за 10 минут ДО incident window
      - incident window: среднее за window_minutes минут вокруг starts_at
    """
    if not settings.CH_PROD_HOST or not settings.CH_PROD_PASSWORD:
        return None

    env = _ns_to_ch_env(namespace)
    # _PROD_ONLY_NOTE. Источник данных ровно один: CH_PROD_HOST, база
    # WOAnalytics — прод-аналитика. Per-env среза в ней нет: в
    # UserActivityMinuteFact колонки окружения не существует (гадать имя =
    # 400 от CH → секция молча пропадает), а отдельного CH под preprod/
    # preupdate/squad копайлоту не выдано. Раньше env использовался ТОЛЬКО в
    # заголовке строки — инцидент в preprod/squad-N получал прод-цифры
    # активных игроков, подписанные чужим окружением, и LLM строил гипотезу
    # «затронуты десятки тысяч игроков» на данных другого кластера.
    # Решение: не подписывать чужие данные — для не-prod окружений секции нет.
    # Когда/если в WOAnalytics появится env-измерение (или отдельный CH на
    # окружение), здесь добавляется WHERE Env = … и снимается этот гейт.
    if env != "prod":
        logger.debug(
            "clickhouse.blast_radius_skipped ns=%s env=%s: prod-only data source",
            namespace, env,
        )
        return None

    win = window_minutes or settings.CH_BLAST_RADIUS_WINDOW_MINUTES
    ts = _parse_ts(starts_at)
    if ts is None:
        logger.debug("clickhouse.blast_radius_unparsed_ts ns=%s raw=%r", namespace, starts_at)
        return None

    half = timedelta(minutes=win // 2)
    baseline_end = ts - half
    baseline_start = baseline_end - timedelta(minutes=10)
    window_start = ts - half
    window_end = ts + half

    def fmt(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    # Интерполируются только datetime-литералы, сформированные через
    # strftime("%Y-%m-%d %H:%M:%S") из распарсенных ISO-строк инцидента.
    # Attacker-controlled значения сюда не попадают, CH HTTP-клиент в
    # этом проекте не поддерживает param-binding для DateTime.
    sql = f"""
SELECT
    Minute,
    countDistinct(DynUserId) AS active_users
FROM UserActivityMinuteFact
WHERE Minute >= toDateTime('{fmt(baseline_start)}')
  AND Minute <= toDateTime('{fmt(window_end)}')
GROUP BY Minute
ORDER BY Minute
"""  # nosec B608 — datetime-only interpolation, see note above

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
