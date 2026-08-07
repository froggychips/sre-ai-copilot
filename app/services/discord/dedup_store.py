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
from sqlalchemy.exc import IntegrityError

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

    Hot-path (enriched + incident alert, бюджет <500ms): ЧИСТЫЙ single-row
    SELECT, без write-amplification. Раньше тут на каждый запрос гонялся
    full-scan `DELETE ... first_ts < cutoff` + commit() (Infra H4) — purge
    вынесен в `purge_stale`, который дёргает beat (см. app/workers/tasks.py
    `discord-dedup-purge`). Свежесть всё равно проверяется по first_ts ниже,
    так что stale-строка до прихода janitor-а остаётся невидимой.
    """
    now = now or time.time()
    cutoff = now - ttl_sec
    try:
        with _pg_session() as db:
            row = db.get(DiscordDedupEntry, key)
            if row is None:
                return None
            # stale-строка (ещё не вычищенная purge_stale) невидима:
            # сверяем first_ts, как и в in-memory fallback ниже.
            if _to_ts(cast(datetime, row.first_ts)) < cutoff:
                return None
            return _row_to_dict(row)
    except Exception as e:
        _fallback(e)
        with _dedup_lock:
            rec = _dedup_state._recent_enriched.get(key)
            if rec is None or (now - rec.get("first_ts", 0)) > ttl_sec:
                return None
            return dict(rec)


def claim(
    key: str,
    ttl_sec: int,
    now: Optional[float] = None,
    *,
    alertname: Optional[str] = None,
    namespace: Optional[str] = None,
    service: Optional[str] = None,
    severity: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Атомарно заклеймить ключ ПЕРЕД POST-ом (закрывает TOCTOU двух реплик).

    Старый цикл get_fresh → POST → save оставлял окно на длину HTTP-roundtrip:
    обе реплики промахивались и обе постили (дубль-@here). Здесь ключ
    резервируется placeholder-строкой (msg_id="") ДО POST-а; атомарность даёт
    PK-конфликт INSERT-а.

    Возвращает:
      - None — claim наш: caller делает POST, затем save() (финализация с
        реальным msg_id) либо release() при неудаче POST-а;
      - dict с непустым msg_id — сообщение уже есть: caller PATCH-ит;
      - dict с пустым msg_id — другая реплика mid-post (или legacy-POST без
        msg_id): caller молча пропускает дубль.
    """
    now = now or time.time()
    # Быстрый путь: свежая запись уже есть (и это же — совместимость с
    # тестами, подменяющими get_fresh).
    existing = get_fresh(key, ttl_sec=ttl_sec, now=now)
    if existing is not None:
        return existing
    try:
        with _pg_session() as db:
            row = DiscordDedupEntry(
                key=key,
                msg_id="",           # placeholder до финализации save()
                webhook_url="",
                embed=None,
                first_ts=_to_dt(now),
                last_ts=_to_dt(now),
                count=1,
                alertname=alertname,
                namespace=namespace,
                service=service,
                severity=severity,
            )
            db.add(row)
            try:
                db.commit()
                return None  # claim наш
            except IntegrityError:
                # Проиграли гонку INSERT-а либо лежит stale-строка.
                db.rollback()
                locked = db.get(DiscordDedupEntry, key, with_for_update=True)
                if locked is None:
                    # Строку выпилил janitor между конфликтом и SELECT —
                    # окно микроскопическое; ведём себя как при свободном
                    # ключе (старое поведение = POST).
                    return None
                if _to_ts(cast(datetime, locked.first_ts)) >= now - ttl_sec:
                    return _row_to_dict(locked)
                # Stale-строка: переклеймливаем окно под себя.
                locked.msg_id = ""
                locked.webhook_url = ""
                locked.embed = None
                locked.first_ts = _to_dt(now)
                locked.last_ts = _to_dt(now)
                locked.count = 1
                locked.alertname = alertname
                locked.namespace = namespace
                locked.service = service
                locked.severity = severity
                db.commit()
                return None
    except Exception as e:
        _fallback(e)
        with _dedup_lock:
            rec = _dedup_state._recent_enriched.get(key)
            if rec is not None and (now - rec.get("first_ts", 0)) <= ttl_sec:
                return dict(rec)
            # Свободно/протухло — клеймим placeholder-ом (под локом = атомарно
            # в рамках процесса; cross-replica гарантии без PG нет, как и
            # раньше у fallback-пути).
            _dedup_state._recent_enriched[key] = {
                "msg_id": "",
                "first_ts": now,
                "last_ts": now,
                "count": 1,
                "webhook_url": "",
                "embed": None,
                "alertname": alertname,
                "namespace": namespace,
                "service": service,
                "severity": severity,
            }
            return None


def release(key: str) -> None:
    """Снять незавершённый claim (POST упал) — освободить ключ.

    Удаляет ТОЛЬКО placeholder (msg_id="") — финализированную запись с
    реальным msg_id не трогаем.
    """
    try:
        with _pg_session() as db:
            row = db.get(DiscordDedupEntry, key, with_for_update=True)
            if row is not None and not row.msg_id:
                db.delete(row)
                db.commit()
            return
    except Exception as e:
        _fallback(e)
        with _dedup_lock:
            rec = _dedup_state._recent_enriched.get(key)
            if rec is not None and not rec.get("msg_id"):
                del _dedup_state._recent_enriched[key]


def purge_stale(ttl_sec: int, now: Optional[float] = None) -> int:
    """Удалить stale-строки (first_ts < now-ttl) одним DELETE. Janitor.

    Вынесено из get_fresh (hot-path), дёргается beat-задачей
    `discord-dedup-purge` (app/workers/tasks.py). Возвращает число
    удалённых строк. PG недоступен → чистим in-memory fallback-кэш.
    """
    now = now or time.time()
    cutoff = _to_dt(now - ttl_sec)
    try:
        with _pg_session() as db:
            deleted = (
                db.query(DiscordDedupEntry)
                .filter(DiscordDedupEntry.first_ts < cutoff)
                .delete(synchronize_session=False)
            )
            db.commit()
            return int(deleted or 0)
    except Exception as e:
        _fallback(e)
        removed = 0
        with _dedup_lock:
            stale = [
                k
                for k, rec in _dedup_state._recent_enriched.items()
                if (now - rec.get("first_ts", 0)) > ttl_sec
            ]
            for k in stale:
                del _dedup_state._recent_enriched[k]
                removed += 1
        return removed


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
