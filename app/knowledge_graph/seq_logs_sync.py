"""Sync error/fatal/warning логов из Seq → kg_log_observations.

Beat-task `kg_seq_logs_sync` каждые ~10 мин:
1. Для каждого настроенного Seq-инстанса (prod / preprod / preupdate)
   запрашивает события за окно `window_minutes` по level=Error/Fatal.
2. Группирует по `Application`/`service`-тэгу.
3. Пытается сматчить каждую группу против `kg_services.name + namespace`
   (если namespace известен из конфигурации инстанса). Не сматчилось —
   пишем строку с `service_id=NULL` (контекст не теряем).
4. Per (service, level, source) пишет одну строку с count, sample
   message и md5 hash самого частого MessageTemplate.

Идемпотентность: UNIQUE(service_id, ts, level, source) +
`ON CONFLICT DO UPDATE count=excluded.count` — повторный tick в том
же окне переписывает count, не плодит дубли.

Конфигурация:
    SEQ_INSTANCES — JSON список `[{"name", "url", "token", "namespace?"}]`
    + удобные одиночные ENV (SEQ_URL_PROD/SEQ_TOKEN_PROD и т.п.) для
    типичных layout'ов WO. См. `_load_instances()`.

CLI: `python -m app.knowledge_graph.seq_logs_sync`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, cast

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.context.seq_client import SeqClient
from app.knowledge_graph.schema import LogObservation, Service
from app.services.pii_redaction import redact_pii

log = logging.getLogger(__name__)


# Уровни Seq, которые льём в KG. Warning тоже учитываем (см. WO signal-m33302),
# но при необходимости можно отрезать через env позже.
_LEVELS = ("Error", "Fatal", "Warning")


def _load_instances() -> List[Dict[str, Optional[str]]]:
    """Список Seq-инстансов из settings.

    Поддерживает два формата:
      1. `SEQ_INSTANCES` (JSON) — `[{"name": "prod", "url": "...",
         "token": "...", "namespace": "prod-shared"}]`.
      2. Одиночные SEQ_URL_<ENV> / SEQ_TOKEN_<ENV> для prod/preprod/preupdate.

    Пустой список = task no-op.
    """
    instances: List[Dict[str, Optional[str]]] = []

    # 1) Полная JSON-конфигурация имеет приоритет (если задана и парсится).
    raw_json = getattr(settings, "SEQ_INSTANCES", "") or ""
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, list):
                for item in parsed:
                    if not isinstance(item, dict) or not item.get("url"):
                        continue
                    instances.append({
                        "name": str(item.get("name") or item.get("url")),
                        "url": str(item["url"]),
                        "token": item.get("token") or None,
                        "namespace": item.get("namespace") or None,
                    })
        except json.JSONDecodeError as e:
            log.warning("seq_logs_sync.bad_seq_instances_json err=%s", e)

    # 2) Fallback на одиночные envs.
    for env_name in ("prod", "preprod", "preupdate"):
        url = getattr(settings, f"SEQ_URL_{env_name.upper()}", "") or ""
        token = getattr(settings, f"SEQ_TOKEN_{env_name.upper()}", "") or ""
        if not url:
            continue
        if any(i["url"] == url for i in instances):
            continue
        instances.append({
            "name": env_name,
            "url": url,
            "token": token or None,
            # namespace mapping не очевиден (на одном Seq живут разные ns) —
            # оставляем None, матчинг по name в любом namespace.
            "namespace": None,
        })

    return instances


def _seq_app_candidates(app_name: str) -> List[str]:
    """Кандидаты k8s-имени сервиса по .NET `App`-тэгу из Seq.

    WO пишет `App` как .NET assembly (recon 2026-06-05):
      "GR.WO.Bot"          → bot-service
      "GR.WO.Push.Service" → push-service
    Правило: убрать префикс `GR.WO.`, отбросить хвост `.Service`, взять
    первый сегмент, lowercase, добавить `-service`. Плюс само сырое имя —
    на случай, если где-то App уже == k8s-имени.
    """
    cands: List[str] = [app_name]
    m = re.match(r"^GR\.WO\.(.+)$", app_name, re.IGNORECASE)
    if m:
        rest = re.sub(r"\.Service$", "", m.group(1), flags=re.IGNORECASE)
        seg = rest.split(".")[0].strip().lower()
        if seg:
            cands.append(f"{seg}-service")
    # dedup с сохранением порядка (dict сохраняет insertion order с 3.7);
    # прежняя идиома `c in seen or seen.add(c)` валила mypy (func-returns-value).
    return list(dict.fromkeys(cands))


def _match_service(
    db: Session,
    app_name: str,
    namespace_hint: Optional[str],
) -> Optional[Service]:
    """Best-effort матч `App`-тэга против kg_services.

    Для каждого кандидата имени (см. `_seq_app_candidates`):
      1. `namespace=hint && name=cand`
      2. Единственный non-synthetic Service с `name=cand` across namespaces
    Первый успех выигрывает. Если ничего — None (service_id=NULL, как раньше).
    """
    for cand in _seq_app_candidates(app_name):
        if namespace_hint:
            svc = (
                db.query(Service)
                .filter(Service.namespace == namespace_hint, Service.name == cand)
                .one_or_none()
            )
            if svc is not None:
                return svc

        candidates = (
            db.query(Service)
            .filter(Service.name == cand, Service.synthetic.is_(False))
            .all()
        )
        if len(candidates) == 1:
            return candidates[0]
    return None


def _msg_hash(msg: str) -> str:
    return hashlib.md5(msg.encode("utf-8", errors="replace"), usedforsecurity=False).hexdigest()  # nosec B324 — content fingerprint, не security


def _upsert_log_obs(
    db: Session,
    *,
    service_id: Optional[int],
    ts: datetime,
    level: str,
    count: int,
    top_message_hash: Optional[str],
    sample_message: Optional[str],
    source: str,
    namespace: Optional[str],
) -> None:
    """INSERT ... ON CONFLICT (service_id, ts, level, source) DO UPDATE count.

    Идемпотентно по уникальному ключу. Пишет также sample/hash на UPDATE —
    повторный tick в том же окне даёт более полную картину.
    """
    stmt = (
        pg_insert(LogObservation.__table__)
        .values(
            service_id=service_id,
            ts=ts,
            level=level,
            count=count,
            top_message_hash=top_message_hash,
            sample_message=sample_message,
            source=source,
            namespace=namespace,
            created_at=datetime.utcnow(),
        )
        .on_conflict_do_update(
            constraint="uq_kg_log_obs_service_ts_level_source",
            set_={
                "count": count,
                "top_message_hash": top_message_hash,
                "sample_message": sample_message,
                "namespace": namespace,
            },
        )
    )
    # SAVEPOINT на строку: при DataError/IntegrityError одна битая строка
    # не переводит Session в aborted-состояние, иначе следующий _upsert_log_obs
    # и финальный db.commit() падали бы с PendingRollbackError, теряя все
    # успешно записанные ранее строки этого tick. begin_nested() автоматически
    # откатывает SAVEPOINT при исключении — оно пробрасывается наверх, где
    # caller логирует seq_logs_sync.upsert_failed.
    with db.begin_nested():
        db.execute(stmt)


async def _sync_instance(
    db: Session,
    instance: Dict[str, Optional[str]],
    since: datetime,
    until: datetime,
    ts_bucket: datetime,
) -> Dict[str, int]:
    """Sync одного Seq-инстанса: за окно [since, until] тянем top-events
    по каждому уровню и пишем агрегаты.
    """
    name = str(instance.get("name") or "unknown")
    url = str(instance.get("url") or "")
    token = instance.get("token") or None
    ns_hint = instance.get("namespace") or None

    client = SeqClient(base_url=url, api_key=token, timeout=10.0)
    stats = {"groups_total": 0, "matched": 0, "unmatched": 0, "rows": 0}

    for level in _LEVELS:
        try:
            events = await client.top_messages(
                level=level, since=since, until=until, limit=500,
            )
        except Exception as e:
            log.warning(
                "seq_logs_sync.fetch_failed source=%s level=%s err=%s",
                name, level, e,
            )
            continue
        if not events:
            continue

        by_app = SeqClient.aggregate_by_service(events)
        for app_name, (total, counter) in by_app.items():
            stats["groups_total"] += 1
            top_msg = ""
            if counter:
                # most_common(1) — самый частый MessageTemplate в окне.
                top_msg, _ = counter.most_common(1)[0]
            top_hash = _msg_hash(top_msg) if top_msg else None

            svc_id: Optional[int] = None
            ns_for_row: Optional[str] = ns_hint
            if app_name:
                svc = _match_service(db, app_name, ns_hint)
                if svc is not None:
                    svc_id = cast(int, svc.id)
                    ns_for_row = cast(str, svc.namespace)
                    stats["matched"] += 1
                else:
                    stats["unmatched"] += 1
            else:
                stats["unmatched"] += 1

            # PII scrub at write-time. Raw stack-traces from Seq may
            # contain emails / IPs / JWTs / bearer tokens / request payloads
            # — they must NOT land in kg_log_observations.sample_message,
            # which is later echoed into Discord embeds. Idempotent: the
            # placeholders we substitute (<email>, <jwt>, ...) don't
            # match the source patterns, so re-running redact is a no-op.
            sample_redacted = redact_pii(top_msg) if top_msg else None

            try:
                _upsert_log_obs(
                    db,
                    service_id=svc_id,
                    ts=ts_bucket,
                    level=level,
                    count=int(total),
                    top_message_hash=top_hash,
                    sample_message=sample_redacted,
                    source=name,
                    namespace=ns_for_row,
                )
                stats["rows"] += 1
            except Exception as e:
                log.warning(
                    "seq_logs_sync.upsert_failed source=%s app=%s level=%s err=%s",
                    name, app_name, level, e,
                )

    return stats


async def _sync_seq_logs_async(
    db: Session,
    window_minutes: int = 10,
) -> Dict[str, Any]:
    instances = _load_instances()
    if not instances:
        log.info("seq_logs_sync.skipped reason=no_instances")
        return {"skipped": "no_instances"}

    until = datetime.utcnow()
    since = until - timedelta(minutes=window_minutes)
    # Бакет ts — округлённый до начала окна, чтобы повторные tick'и в
    # пределах окна писали в ту же строку через ON CONFLICT.
    bucket_minutes = (since.minute // window_minutes) * window_minutes
    ts_bucket = since.replace(minute=bucket_minutes, second=0, microsecond=0)

    totals = {"instances": len(instances), "rows": 0, "matched": 0, "unmatched": 0}
    for inst in instances:
        try:
            stats = await _sync_instance(db, inst, since, until, ts_bucket)
        except Exception as e:
            log.warning(
                "seq_logs_sync.instance_failed name=%s err=%s",
                inst.get("name"), e,
            )
            continue
        totals["rows"] += stats["rows"]
        totals["matched"] += stats["matched"]
        totals["unmatched"] += stats["unmatched"]

    db.commit()
    log.info(
        "seq_logs_sync.done instances=%d rows=%d matched=%d unmatched=%d "
        "window=%dm bucket=%s",
        totals["instances"], totals["rows"], totals["matched"],
        totals["unmatched"], window_minutes, ts_bucket.isoformat(),
    )
    return totals


def sync_seq_logs(db: Session, window_minutes: int = 10) -> Dict[str, Any]:
    """Sync-обёртка для Celery."""
    return asyncio.run(_sync_seq_logs_async(db, window_minutes=window_minutes))


if __name__ == "__main__":
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        print(sync_seq_logs(db))
    finally:
        db.close()
