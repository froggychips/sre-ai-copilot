"""In-memory dedup state for Discord incident embeds.

Кэш per-process, TTL 30 минут. Используется DiscordService для двух стратегий:
  * (#1) точечный дедуп по (alertname, ns, pod) — PATCH embed-а вместо
    нового POST когда тот же инцидент срабатывает повторно.
  * (#9) burst-агрегация по alertname — если ≥_LINKED_MIN_COUNT срабатываний
    за _LINKED_WINDOW_SEC, сворачиваем в одно сообщение с группой ns/pod.

State хранится на module-level — DiscordService может вызываться из разных
asyncio-тасков (FastAPI handlers + Celery workers держат свой state в каждом
процессе; кэш per-process — это OK для канареечной фазы).

Тесты сбрасывают кэш через `discord_service._recent_incidents.clear()` —
это работает благодаря re-export'у в shim'е верхнего уровня.
"""

import re
import threading
import time
from typing import Any, Dict, Optional, Tuple

_DEDUP_TTL_SEC = 30 * 60
_LINKED_WINDOW_SEC = 5 * 60
_LINKED_MIN_COUNT = 3

_recent_incidents: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
_recent_by_alertname: Dict[Tuple[str], Dict[str, Any]] = {}
_dedup_lock = threading.Lock()


def _purge_dedup_state(now: Optional[float] = None) -> None:
    """Удалить из обоих кэшей записи старше TTL. Вызывается при каждом insert."""
    now = now or time.time()
    cutoff = now - _DEDUP_TTL_SEC
    for k in list(_recent_incidents.keys()):
        if _recent_incidents[k].get("first_ts", 0) < cutoff:
            del _recent_incidents[k]
    for ka in list(_recent_by_alertname.keys()):
        if _recent_by_alertname[ka].get("first_ts", 0) < cutoff:
            del _recent_by_alertname[ka]


def _webhook_edit_endpoint(url: str, message_id: str) -> Optional[str]:
    """Из webhook-URL вида https://discord.com/api/webhooks/{id}/{token}
    собрать endpoint для PATCH /messages/{message_id}.

    Поддерживаем как `discord.com/api/webhooks/...` так и
    `canary.discordapp.com/api/v10/webhooks/...`. Query-параметры (`wait=true`)
    отрезаем — на /messages/{id} они не нужны.
    """
    if not url:
        return None
    base = url.split("?", 1)[0].rstrip("/")
    m = re.search(r"(.+/webhooks/\d+/[^/]+)$", base)
    if not m:
        return None
    return f"{m.group(1)}/messages/{message_id}"
