"""SQLAlchemy ORM для Phase A — единственная таблица `kg_remediation_decisions`.

В Phase A нужна ОДНА table, чтобы не раздуть миграцию до executor'а
(который не реализован). Триплет actions/observations/approvals — в Phase B+.

Записи в этой таблице — pure preview (`status = preview_only`). Идемпотентность
по `(incident_id, idempotency_key)` UNIQUE — повтор того же incident+playbook
не плодит дубли.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import mapped_column
from sqlalchemy import (JSON, DateTime, Integer, String, Text,
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

    id = mapped_column(Integer, primary_key=True)
    # Без `index=True`: uq_kg_remediation_decisions_incident_idem
    # (incident_id, idempotency_key) покрывает эту колонку как префикс.
    incident_id = mapped_column(String, nullable=True)
    alert_fingerprint = mapped_column(String, nullable=True, index=True)
    target_ref = mapped_column(JSON, nullable=True)
    classification = mapped_column(String, nullable=True, index=True)
    classification_provenance = mapped_column(JSON, nullable=True)
    risk_axes = mapped_column(JSON, nullable=True)
    candidate_playbooks = mapped_column(JSON, nullable=True)
    selected_playbook = mapped_column(String, nullable=True, index=True)
    # Enum по значению: 'auto' | 'approve' | 'block'.
    decision = mapped_column(String, nullable=True, index=True)
    decision_reasons = mapped_column(JSON, nullable=True)
    command_preview = mapped_column(Text, nullable=True)
    idempotency_key = mapped_column(String, nullable=False, index=True)
    created_at = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "incident_id", "idempotency_key",
            name="uq_kg_remediation_decisions_incident_idem",
        ),
        # Отдельного индекса по (incident_id, idempotency_key) нет
        # намеренно: ровно этот состав держит UniqueConstraint выше.
    )
