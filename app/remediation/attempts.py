"""Попытки исполнения remediation: `kg_remediation_attempts`.

Состояние исполнителя жило в `incidents.analysis`: `executor_in_flight`
(claim), `executor_applied` (итог), `executor_state_unknown`,
`executor_verification`. От двойной записи в кластер защищали row-lock
`SELECT … FOR UPDATE` на строке инцидента и проверка ключей JSON. Работало,
но жизненный цикл приходилось выковыривать из блоба, а гарантия держалась на
том, что каждый писатель analysis аккуратно мержит, а не заменяет его
(`pipeline._persist` однажды уже стирал `executor_applied` при re-fire).

Здесь одна строка на пару (incident_id, signature) — ровно как одобрение в
`kg_action_approvals`: одна команда, одно одобрение, одна попытка. UNIQUE по
этой паре делает claim вставкой строки: второй претендент получает конфликт
уникальности от самой БД, даже если оба прошли проверки одновременно (на
SQLite `FOR UPDATE` вообще не существует).

Жизненный цикл (`status`):

    claimed ──► applied ──► verified
       │           └──────► verification_failed
       ├──────► failed            (kubectl вернул ошибку — терминально)
       └──────► unknown ──► claimed   (протухший claim; обратно — только
                                       по одобрению, выданному позже пометки)

Отказы до claim-а строк не создают: строка занимает уникальный ключ, и
транзиентный отказ (моргнувший пере-dry-run) навсегда закрыл бы действие.
Они остаются в audit-логе (`EXECUTOR_APPLY_REFUSED`). `dry_run_ok` — стадия
пайплайна, её держит `analysis.executor_result`, строки тоже не порождает.

JSON-ключи в analysis исполнитель продолжает писать: их читают Discord-embed,
timeline и отчёты. Источник истины для решения «можно ли писать в кластер» —
эта таблица (плюс JSON для записей, сделанных до неё).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import (JSON, Column, DateTime, Index, Integer, String, Text,
                        UniqueConstraint, update)

from app.database import Base

STATUS_CLAIMED = "claimed"
STATUS_APPLIED = "applied"
STATUS_FAILED = "failed"
STATUS_VERIFIED = "verified"
STATUS_VERIFICATION_FAILED = "verification_failed"
STATUS_UNKNOWN = "unknown"

#: Запись в кластер состоялась (успешно или нет) — повтор запрещён.
DONE_STATUSES = frozenset(
    {STATUS_APPLIED, STATUS_FAILED, STATUS_VERIFIED, STATUS_VERIFICATION_FAILED}
)
#: После записи: отложенная верификация пишет исход сюда.
APPLIED_STATUSES = frozenset(
    {STATUS_APPLIED, STATUS_VERIFIED, STATUS_VERIFICATION_FAILED}
)


def _utcnow_naive() -> datetime:
    """Naive UTC — формат DateTime-колонок остальных kg_*-таблиц."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class RemediationAttempt(Base):
    """Одна попытка исполнения утверждённого ExecutionIntent.

    Колонки:
    - `incident_id`, `signature`: пара, по которой выдано одобрение
      (`kg_action_approvals`); UNIQUE — claim = INSERT.
    - `action` / `namespace` / `resource_name`: копия из intent-а для
      фильтров аудита без разбора JSON.
    - `intent`: снимок ExecutionIntent, по которому шла запись.
    - `status`: см. жизненный цикл в докстринге модуля.
    - `claimed_at` / `applied_at`: когда взят claim и когда записан итог.
      TTL claim-а считается от `claimed_at`.
    - `result`: то же, что `analysis.executor_applied` (команда, вывод,
      снимки идентичности, решение gate-а).
    - `verification`: последняя отложенная проверка (+5/+15 мин).
    - `error`: почему попытка ушла в failed/unknown.
    """
    __tablename__ = "kg_remediation_attempts"

    id = Column(Integer, primary_key=True)
    # Без index=True: uq_kg_remediation_attempts_incident_signature покрывает
    # incident_id как префикс.
    incident_id = Column(String, nullable=False)
    signature = Column(String, nullable=False)
    action = Column(String, nullable=True)
    namespace = Column(String, nullable=True)
    resource_name = Column(String, nullable=True)
    intent = Column(JSON, nullable=True)
    status = Column(String, nullable=False)
    applied_by = Column(String, nullable=True)
    claimed_at = Column(DateTime, nullable=True)
    applied_at = Column(DateTime, nullable=True)
    result = Column(JSON, nullable=True)
    verification = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utcnow_naive, nullable=False)
    updated_at = Column(DateTime, default=_utcnow_naive, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "incident_id", "signature",
            name="uq_kg_remediation_attempts_incident_signature",
        ),
        # Аудит «что зависло/провалилось за период»: фильтр по статусу и времени.
        Index("ix_kg_remediation_attempts_status_updated", "status", "updated_at"),
    )


def blocking_attempt(db: Any, incident_id: str) -> Optional[RemediationAttempt]:
    """Последняя попытка по инциденту, которая запрещает новую запись.

    Инвариант прежней схемы — «не больше одной записи в кластер на
    инцидент»: `executor_applied` переживал re-fire и блокировал любой
    следующий intent. Уникальность по (incident, signature) сама по себе
    слабее — новый intent после re-fire дал бы новую пару, — поэтому
    блокировку ищем по инциденту целиком.
    """
    return (
        db.query(RemediationAttempt)
        .filter(RemediationAttempt.incident_id == incident_id)
        .order_by(RemediationAttempt.id.desc())
        .first()
    )


def latest_applied(db: Any, incident_id: str) -> Optional[RemediationAttempt]:
    """Попытка, дошедшая до записи в кластер, — её проверяет верификация."""
    return (
        db.query(RemediationAttempt)
        .filter(
            RemediationAttempt.incident_id == incident_id,
            RemediationAttempt.status.in_(sorted(APPLIED_STATUSES)),
        )
        .order_by(RemediationAttempt.id.desc())
        .first()
    )


def claim_is_fresh(row: RemediationAttempt, ttl_seconds: int) -> bool:
    """Жив ли claim. Нет времени — считаем живым (fail-closed): пока TTL
    определить нельзя, повторный kubectl запрещён — как у JSON-claim-а."""
    claimed = row.claimed_at
    if not isinstance(claimed, datetime):
        return True
    if claimed.tzinfo is not None:
        claimed = claimed.astimezone(timezone.utc).replace(tzinfo=None)
    return (_utcnow_naive() - claimed).total_seconds() <= ttl_seconds


def new_claim(
    incident_id: str,
    signature: str,
    intent: Dict[str, Any],
    applied_by: str,
) -> RemediationAttempt:
    """Строка claim-а. Добавляется в сессию вызывающим и коммитится вместе
    с JSON-claim-ом: конфликт уникальности на commit = проигранная гонка."""
    now = _utcnow_naive()
    return RemediationAttempt(
        incident_id=incident_id,
        signature=signature,
        action=intent.get("action"),
        namespace=intent.get("namespace"),
        resource_name=intent.get("resource_name"),
        intent=intent,
        status=STATUS_CLAIMED,
        applied_by=applied_by,
        claimed_at=now,
        created_at=now,
        updated_at=now,
    )


def reclaim_unknown(db: Any, row: RemediationAttempt, applied_by: str) -> bool:
    """unknown → claimed compare-and-swap. False — строку уже перехватили.

    Условный UPDATE, а не присваивание атрибута: два претендента, прочитавшие
    unknown одновременно, не должны оба получить claim.
    """
    now = _utcnow_naive()
    res = db.execute(
        update(RemediationAttempt)
        .where(
            RemediationAttempt.id == row.id,
            RemediationAttempt.status == STATUS_UNKNOWN,
        )
        .values(
            status=STATUS_CLAIMED, claimed_at=now, applied_by=applied_by,
            error=None, updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if getattr(res, "rowcount", 0) != 1:
        return False
    # synchronize_session=False не трогает объект в памяти — перечитываем,
    # чтобы дальнейшие переходы видели claimed, а не прежний unknown.
    db.refresh(row)
    return True


def set_status(row: Any, status: str, **fields: Any) -> None:
    """Перевести попытку в новый статус (commit — за вызывающим) и учесть
    переход в метрике."""
    from app.observability.ai_metrics import track_remediation_attempt_transition

    previous = row.status
    row.status = status
    for key, value in fields.items():
        setattr(row, key, value)
    row.updated_at = _utcnow_naive()
    track_remediation_attempt_transition(previous, status)
