"""Cross-replica store для enriched PATCH-dedup.

Контекст (2026-06-10): sre-ai-api крутится в 2 репликах, а
`_recent_enriched` в dedup.py — per-process dict. AM-вебхук балансится
между подами → ~50% промахов дедупа → дубль-POST critical-алерта с
повторным mention (прецедент: PreprodRestartsSpike 16:16 и 16:31).

Решение — состояние в Postgres (та же БД, что KG): UPSERT по key,
TTL сравнением first_ts. Postgres недоступен / таблица не накачена
миграцией → прозрачный fallback на старый in-memory dict (deduplication
хуже, чем была бы с PG, но не хуже текущего поведения; постинг не падает).

Sync-вызовы из async-кода: каждый запрос — короткий single-row UPSERT по
PK, на фоне HTTP-roundtrip'а к Discord это шум.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, cast

from sqlalchemy import Column, DateTime, Integer, String, JSON

from app.database import Base
from . import dedup as _dedup_state
from .dedup import _dedup_lock

log = logging.getLogger(__name__)

# Один warning на процесс при уходе в fallback — иначе каждый алерт
# при лежащем PG зальёт лог.
_pg_warned = False


class DiscordDedupEntry(Base):
    """Строка PATCH-dedup: один Discord-message на content-key в TTL-окне."""

    __tablename__ = "discord_dedup"

    key = Column(String(40), primary_key=True)  # sha1 hex от _compute_enriched_key
    msg_id = Column(String(32), nullable=False)
    webhook_url = Column(String(512), nullable=False)
    embed = Column(JSON, nullable=True)
    first_ts = Column(DateTime, nullable=False, index=True)
    last_ts = Column(DateTime, nullable=False)
    count = Column(Integer, nullable=False, default=1)
    # Debug-видимость: что за алерт скрывается за sha1-ключом.
    alertname = Column(String(255), nullable=True)
    namespace = Column(String(255), nullable=True)
    service = Column(String(255), nullable=True)
    severity = Column(String(32), nullable=True)


def _to_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


def _to_ts(dt: datetime) -> float:
    return dt.replace(tzinfo=timezone.utc).timestamp()


def _row_to_dict(row: DiscordDedupEntry) -> Dict[str, Any]:
    return {
        "msg_id": row.msg_id,
        "webhook_url": row.webhook_url,
        "embed": row.embed,
        # cast: на инстансе ORM-атрибуты — datetime; стабы SQLAlchemy после
        # bump'а (kubernetes/starlette deps, 2026-06-16) стали инферить их
        # как Column[datetime] → mypy arg-type. Рантайм-тип корректен.
        "first_ts": _to_ts(cast(datetime, row.first_ts)),
        "last_ts": _to_ts(cast(datetime, row.last_ts)),
        "count": row.count,
    }


def _pg_session():
    from app.database import SessionLocal  # noqa: PLC0415 — lazy, тесты без PG

    return SessionLocal()


def _fallback(e: Exception) -> None:
    global _pg_warned
    if not _pg_warned:
        log.warning("dedup_store.pg_unavailable_fallback_memory", exc_info=e)
        _pg_warned = True


def get_fresh(key: str, ttl_sec: int, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Вернуть запись, если она в TTL-окне; stale-записи невидимы.

    Заодно opportunistic purge: protухшие строки удаляются (дешевле, чем
    отдельный janitor; таблица всегда в пределах активных групп алертов).
    """
    now = now or time.time()
    cutoff = _to_dt(now - ttl_sec)
    try:
        with _pg_session() as db:
            (
                db.query(DiscordDedupEntry)
                .filter(DiscordDedupEntry.first_ts < cutoff)
                .delete(synchronize_session=False)
            )
            db.commit()
            row = db.get(DiscordDedupEntry, key)
            if row is None:
                return None
            return _row_to_dict(row)
    except Exception as e:
        _fallback(e)
        with _dedup_lock:
            rec = _dedup_state._recent_enriched.get(key)
            if rec is None or (now - rec.get("first_ts", 0)) > ttl_sec:
                return None
            return dict(rec)


def bump(key: str, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """count++ и last_ts=now атомарно; вернуть обновлённую запись.

    None — запись исчезла между get_fresh и bump (purge/race): caller
    делает обычный POST.
    """
    now = now or time.time()
    try:
        with _pg_session() as db:
            row = db.get(DiscordDedupEntry, key, with_for_update=True)
            if row is None:
                return None
            row.count = (row.count or 1) + 1
            row.last_ts = _to_dt(now)
            db.commit()
            return _row_to_dict(row)
    except Exception as e:
        _fallback(e)
        with _dedup_lock:
            rec = _dedup_state._recent_enriched.get(key)
            if rec is None:
                return None
            rec["count"] = rec.get("count", 1) + 1
            rec["last_ts"] = now
            return dict(rec)


def update_embed(key: str, embed: Dict[str, Any]) -> None:
    """Обновить закешированный embed после успешного PATCH (footer-история)."""
    try:
        with _pg_session() as db:
            row = db.get(DiscordDedupEntry, key)
            if row is not None:
                row.embed = embed
                db.commit()
            return
    except Exception as e:
        _fallback(e)
        with _dedup_lock:
            if key in _dedup_state._recent_enriched:
                _dedup_state._recent_enriched[key]["embed"] = embed


def save(
    key: str,
    *,
    msg_id: str,
    webhook_url: str,
    embed: Optional[Dict[str, Any]],
    alertname: Optional[str] = None,
    namespace: Optional[str] = None,
    service: Optional[str] = None,
    severity: Optional[str] = None,
    now: Optional[float] = None,
) -> None:
    """Зафиксировать свежий POST. UPSERT: stale-строка с тем же ключом
    перезаписывается с count=1 (это новое окно, не recurrence)."""
    now = now or time.time()
    try:
        with _pg_session() as db:
            row = db.get(DiscordDedupEntry, key)
            if row is None:
                row = DiscordDedupEntry(key=key)
                db.add(row)
            row.msg_id = msg_id
            row.webhook_url = webhook_url
            row.embed = embed
            row.first_ts = _to_dt(now)
            row.last_ts = _to_dt(now)
            row.count = 1
            row.alertname = alertname
            row.namespace = namespace
            row.service = service
            row.severity = severity
            db.commit()
            return
    except Exception as e:
        _fallback(e)
        with _dedup_lock:
            _dedup_state._recent_enriched[key] = {
                "msg_id": msg_id,
                "first_ts": now,
                "last_ts": now,
                "count": 1,
                "webhook_url": webhook_url,
                "embed": embed,
                "alertname": alertname,
                "namespace": namespace,
                "service": service,
                "severity": severity,
            }
