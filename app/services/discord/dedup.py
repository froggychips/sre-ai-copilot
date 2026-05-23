"""In-memory dedup state for Discord incident embeds.

Кэш per-process, TTL 30 минут. Используется DiscordService для двух стратегий:
  * (#1) точечный дедуп по content-key (alertname, ns, service, reason) —
    PATCH embed-а вместо нового POST, когда логически тот же инцидент
    срабатывает повторно. Раньше ключом была AM fingerprint через
    (alertname, ns, pod), но AM минтит свежий fingerprint при каждой
    ре-mission — содержательно тот же инцидент шёл как новый POST.
    Content-key решает: один alertname+service+reason → один embed.
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

# Ключ — строка, сформированная _compute_content_key(). Раньше был
# Tuple[str,str,str] = (alertname, ns, pod); тип сменился, но семантика
# та же (dict lookup в кэше per-process, никаких persistence).
_recent_incidents: Dict[str, Dict[str, Any]] = {}
_recent_by_alertname: Dict[Tuple[str], Dict[str, Any]] = {}
_dedup_lock = threading.Lock()


def _compute_content_key(
    alertname: str,
    namespace: Optional[str],
    service_name: Optional[str],
    reason: Optional[str] = None,
    metric_source: Optional[str] = None,
) -> Optional[str]:
    """Сформировать content-based dedup key.

    Логика:
      * `alertname` обязателен (без него возвращаем None — caller сделает
        fallback на fingerprint).
      * `service_name` обязателен. Если сервис не резолвится, но дан
        `metric_source` — используем `<synthetic:metric_source>` чтобы
        разные «безымянные» инциденты от разных источников не схлопывались.
        Без обоих → None (fallback на fingerprint).
      * `reason` — нормализованный k8s pod_event reason (OOMKilled,
        BackOff, ...). Если None — берём lowercase alertname.

    Формат: `<alertname>:<ns>:<service>:<reason>` для читаемости в дебаг-логах.
    `None`-компоненты в ключе заменяются на `<none>`; иначе lookup может
    столкнуться с pythonовским `None`-ом в исходных данных.
    """
    if not alertname:
        return None
    resolved_service = service_name or (
        f"<synthetic:{metric_source}>" if metric_source else None
    )
    if not resolved_service:
        return None
    ns = namespace or "<none>"
    norm_reason = (reason or alertname).lower()
    return f"{alertname}:{ns}:{resolved_service}:{norm_reason}"


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
