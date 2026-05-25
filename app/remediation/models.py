"""SQLAlchemy ORM для Phase A — единственная таблица `kg_remediation_decisions`.

В Phase A нужна ОДНА table, чтобы не раздуть миграцию до executor'а
(который не реализован). Триплет actions/observations/approvals — в Phase B+.

Записи в этой таблице — pure preview (`status = preview_only`). Идемпотентность
по `(incident_id, idempotency_key)` UNIQUE — повтор того же incident+playbook
не плодит дубли.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (JSON, Column, DateTime, Index, Integer, String, Text,
                        UniqueConstraint)

from app.database import Base


class RemediationDecision(Base):
    """Audit log одного решения copilot-а («что бы я сделал»).

    В Phase A executor отсутствует — поле `decision` принимает auto/approve/
    block, но НИ ОДНО auto не приводит к kubectl-команде. Это даёт fearless
    replay на исторических alert-ах и матрицу «что бы система решила».

    Колонки:
    - `incident_id`: тот же incident_id, что в `incidents` (FK не делаем,
      т.к. incident может быть удалён, а decision history нужна для аудита).
    - `alert_fingerprint`: для cross-link c AlertManager fingerprint'ом.
    - `target_ref`: JSON копия TargetRef.to_dict() — snapshot resolved ресурса.
    - `classification`: `Classification` enum значение (str).
    - `classification_provenance`: `{rule_id, signals_used, confidence_hint}`.
    - `risk_axes`: JSON RiskAxes.to_dict() — 8 discrete enums.
    - `candidate_playbooks`: список имён playbook-ов, у которых match сработал.
    - `selected_playbook`: имя playbook'а, который реально брался для decision.
    - `decision`: `auto` | `approve` | `block`.
    - `decision_reasons`: PolicyDecision.reasons — structured audit.
    - `command_preview`: текстовое представление команды (rendered, НЕ run).
    - `idempotency_key`: SHA-like ключ, уникальный в рамках incident.
    """
    __tablename__ = "kg_remediation_decisions"

    id = Column(Integer, primary_key=True, index=True)
    incident_id = Column(String, nullable=True, index=True)
    alert_fingerprint = Column(String, nullable=True, index=True)
    target_ref = Column(JSON, nullable=True)
    classification = Column(String, nullable=True, index=True)
    classification_provenance = Column(JSON, nullable=True)
    risk_axes = Column(JSON, nullable=True)
    candidate_playbooks = Column(JSON, nullable=True)
    selected_playbook = Column(String, nullable=True, index=True)
    # Enum по значению: 'auto' | 'approve' | 'block'.
    decision = Column(String, nullable=True, index=True)
    decision_reasons = Column(JSON, nullable=True)
    command_preview = Column(Text, nullable=True)
    idempotency_key = Column(String, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "incident_id", "idempotency_key",
            name="uq_kg_remediation_decisions_incident_idem",
        ),
        Index(
            "ix_kg_remediation_decisions_incident_idem",
            "incident_id", "idempotency_key",
        ),
    )
