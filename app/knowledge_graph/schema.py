"""SQLAlchemy схема knowledge-graph узлов и рёбер.

Все таблицы используют тот же Base/engine, что и IncidentRecord
(см. app/database.py) — одна БД, одна миграция Alembic.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (JSON, Boolean, Column, DateTime, ForeignKey, Index,
                        Integer, String, UniqueConstraint)
from sqlalchemy.orm import relationship

from app.database import Base


class Service(Base):
    """Узел графа: микросервис / deployment в k8s.

    `name` — стабильный slug (например, `town-service`), уникален в
    пределах namespace. Сервис может присутствовать в нескольких
    namespace (squad-1, squad-2) — это разные строки.
    """
    __tablename__ = "kg_services"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True)
    namespace = Column(String, nullable=False, index=True)
    team_owner = Column(String, nullable=True)   # squad, например `squad-gd`
    metadata_json = Column(JSON, nullable=True)  # labels, репо, runbook URL...
    # Synthetic = по дизайну никогда не имеет edges (cron-backups, nats-tools,
    # observability-exporters). Исключается из Orphan %-метрики в kg_quality.
    synthetic = Column(Boolean, nullable=False, default=False, server_default="false")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("namespace", "name", name="uq_kg_service_ns_name"),
    )


class Deployment(Base):
    """Узел графа: один rollout/build конкретного сервиса.

    Один сервис → много deployments в истории. Используется RecentDeployRule
    для «деплой за ≤60 минут до alert-а?».
    """
    __tablename__ = "kg_deployments"

    id = Column(Integer, primary_key=True, index=True)
    service_id = Column(Integer, ForeignKey("kg_services.id"), nullable=False, index=True)
    sha = Column(String, nullable=True)
    repo = Column(String, nullable=True)
    buildtype_id = Column(String, nullable=True)  # TeamCity build type
    build_number = Column(String, nullable=True)
    started_at = Column(DateTime, nullable=False, index=True)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String, nullable=True)        # SUCCESS / FAILURE / RUNNING
    triggered_by = Column(String, nullable=True)
    extras = Column(JSON, nullable=True)

    service = relationship("Service")

    __table_args__ = (
        Index("ix_kg_deploy_service_time", "service_id", "started_at"),
    )


class AlertEvent(Base):
    """Узел графа: один alert от alertmanager.

    Дублирует часть данных IncidentRecord, но индексируется по
    (service_id, fired_at) — для запросов upstream/nearby по графу.
    """
    __tablename__ = "kg_alerts"

    id = Column(Integer, primary_key=True, index=True)
    service_id = Column(Integer, ForeignKey("kg_services.id"), nullable=True, index=True)
    alertname = Column(String, nullable=False, index=True)
    severity = Column(String, nullable=True)
    fingerprint = Column(String, nullable=True, unique=True, index=True)
    fired_at = Column(DateTime, nullable=False, index=True)
    resolved_at = Column(DateTime, nullable=True)
    incident_id = Column(String, nullable=True, index=True)  # связь с IncidentRecord
    raw = Column(JSON, nullable=True)

    service = relationship("Service")

    __table_args__ = (
        Index("ix_kg_alert_service_time", "service_id", "fired_at"),
    )


class ServiceEdge(Base):
    """Ребро графа: src сервис вызывает / зависит от dst сервиса.

    `kind` — тип зависимости:
        * `calls`        — синхронный HTTP/gRPC
        * `consumes`     — kafka/queue topic
        * `reads_from`   — DB / cache
        * `runs_on`      — pod на этой ноде / cluster
    Граф направленный: A `calls` B → «A падает, если B недоступен».
    upstream_of(A) = сервисы, от которых A зависит = dst у edge с src=A.
    """
    __tablename__ = "kg_service_edges"

    id = Column(Integer, primary_key=True, index=True)
    src_id = Column(Integer, ForeignKey("kg_services.id"), nullable=False, index=True)
    dst_id = Column(Integer, ForeignKey("kg_services.id"), nullable=False, index=True)
    kind = Column(String, nullable=False)
    weight = Column(Integer, default=1)          # «жирность» edge: % трафика, важность
    discovered_by = Column(String, nullable=True)  # populator/method, для отладки
    extras = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    src = relationship("Service", foreign_keys=[src_id])
    dst = relationship("Service", foreign_keys=[dst_id])

    __table_args__ = (
        UniqueConstraint("src_id", "dst_id", "kind", name="uq_kg_edge_src_dst_kind"),
    )
