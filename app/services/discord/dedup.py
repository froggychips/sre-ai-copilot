"""In-memory dedup state for Discord incident embeds.

Кэш per-process, TTL 30 минут. Используется DiscordService для трёх стратегий:
  * (#1) точечный дедуп по content-key (alertname, ns, service, reason) —
    PATCH embed-а вместо нового POST, когда логически тот же инцидент
    срабатывает повторно. Раньше ключом была AM fingerprint через
    (alertname, ns, pod), но AM минтит свежий fingerprint при каждой
    ре-mission — содержательно тот же инцидент шёл как новый POST.
    Content-key решает: один alertname+service+reason → один embed.
  * (#9) burst-агрегация по alertname — если ≥_LINKED_MIN_COUNT срабатываний
    за _LINKED_WINDOW_SEC, сворачиваем в одно сообщение с группой ns/pod.
  * (Stage 2) PATCH-dedup для `send_enriched_alert` — параллельный кэш
    `_recent_enriched`, ключ sha1(alertname, ns, service, severity). Был
    основной источник тройных постов в #infra-error: preprod AM,
    group_interval=10m, repeat=4h → 18 embed/сутки на одну и ту же группу.

State хранится на module-level — DiscordService может вызываться из разных
asyncio-тасков (FastAPI handlers + Celery workers держат свой state в каждом
процессе; кэш per-process — это OK для канареечной фазы).

Тесты сбрасывают кэш через `discord_service._recent_incidents.clear()` —
это работает благодаря re-export'у в shim'е верхнего уровня.
"""

import hashlib
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
# Stage 2: отдельный кэш для send_enriched_alert. Параллельный — чтобы
# enriched-канал (KG-deterministic) и incident-канал (suspect deploy /
# self-health) дедупились независимо. Семантика та же — sha1-ключ,
# entry хранит msg_id+first_ts+last_ts+count+webhook_url+embed.
_recent_enriched: Dict[str, Dict[str, Any]] = {}
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


def _incident_dedup_key(content_key: str) -> str:
    """Sha1(40 hex) от incident content-key для cross-replica dedup_store.

    Incident-канал переведён с per-process dict (`_recent_incidents`) на общий
    PG-store `discord_dedup` (как enriched-канал) — per-process кэш ломался на
    2+ репликах api: дубль critical-POST с mention. `key` в таблице — String(40),
    поэтому content-key (читаемая строка `alertname:ns:service:reason`) хэшируем
    в sha1. Префикс `incident|` отделяет namespace incident-канала от
    enriched-ключей в той же таблице.
    """
    raw = f"incident|{content_key}".encode("utf-8")
    return hashlib.sha1(raw, usedforsecurity=False).hexdigest()  # nosec B324 — dedup-key, не security


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


def _compute_enriched_key(
    alertname: str,
    namespace: Optional[str],
    service_name: Optional[str],
    severity: Optional[str],
) -> Optional[str]:
    """Sha1-ключ для PATCH-dedup в send_enriched_alert.

    Состав ключа: (alertname, namespace, service, severity). Раздельно от
    incident-кэша (`_compute_content_key`), потому что:
      * enriched-batch не имеет pod_event_reason и шлёт один embed на
        AM-batch (≥1 алерт того же типа в группе ns/service);
      * severity критичен — preprod-warning и preprod-critical не должны
        схлопываться в один embed.

    None-компоненты заменяются на `<none>` (как и в content-key) чтобы
    избежать pythonовского None в hash-input. Возвращает hex sha1 (40 chars)
    для компактности и стабильности (vs ad-hoc f-string).
    """
    if not alertname:
        return None
    ns = namespace or "<none>"
    svc = service_name or "<none>"
    sev = (severity or "<none>").lower()
    raw = f"{alertname}|{ns}|{svc}|{sev}".encode("utf-8")
    return hashlib.sha1(raw, usedforsecurity=False).hexdigest()  # nosec B324 — content-key dedup, не security


def _purge_enriched_state(now: Optional[float] = None, ttl_sec: Optional[int] = None) -> None:
    """Удалить из `_recent_enriched` записи старше ttl_sec.

    ttl_sec может быть config-driven (`ENRICHED_DEDUP_WINDOW_SECONDS`) и
    отличается от `_DEDUP_TTL_SEC` (incident-кэш). По умолчанию 30 мин,
    т.к. AM preprod group_interval=10m → за 30 мин укладывается 3 ре-mission.
    """
    now = now or time.time()
    cutoff = now - (ttl_sec if ttl_sec is not None else _DEDUP_TTL_SEC)
    for k in list(_recent_enriched.keys()):
        if _recent_enriched[k].get("first_ts", 0) < cutoff:
            del _recent_enriched[k]


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
