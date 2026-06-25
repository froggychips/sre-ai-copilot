"""Единый источник engine / SessionLocal / Base для всего приложения.

Все модули обязаны импортировать отсюда. Параллельные обёртки в app/db/*
удалены — наличие двух SessionLocal вело к утечкам соединений и race
между Celery worker-ом и FastAPI handler-ом.
"""
from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

# Пул-параметры применимы только к серверным БД (Postgres). У sqlite
# дефолтный пул — SingletonThreadPool/StaticPool, и pool_size/max_overflow/
# pool_timeout/pool_recycle ему передавать нельзя (TypeError на части
# реализаций / бессмысленно). Тесты гоняют на sqlite — отделяем явно.
_is_sqlite = settings.DATABASE_URL.startswith("sqlite")

_pool_kwargs: dict = {}
if not _is_sqlite:
    # Под нагрузкой sync-сессии гоняются через run_in_threadpool (Starlette,
    # до ~40 потоков) + Celery beat-задачи + dedup_store. Дефолтный QueuePool
    # (pool_size=5/max_overflow=10 → потолок 15) → `QueuePool limit ... timed
    # out`. Поднимаем потолок до 40 одновременных коннектов.
    _pool_kwargs = {
        "pool_size": 20,
        "max_overflow": 20,
        "pool_timeout": 30,
        # Профилактически пересоздаём коннекты раз в 30 мин — k8s LB/PG
        # могут молча рвать долгоживущие idle-соединения.
        "pool_recycle": 1800,
    }

engine = create_engine(
    settings.DATABASE_URL,
    echo=False,
    # pool_pre_ping вытаскивает stale connections при возврате из пула —
    # обязателен в k8s, где DB pod может рестартиться без уведомления.
    pool_pre_ping=True,
    **_pool_kwargs,
)
SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    # expire_on_commit=False — иначе после commit() ORM-объекты надо
    # перевычитывать. В Celery-task-ах это лишний roundtrip.
    expire_on_commit=False,
)
Base = declarative_base()


class IncidentRecord(Base):
    __tablename__ = "incidents"
    id = Column(Integer, primary_key=True, index=True)
    incident_id = Column(String, unique=True, index=True)
    status = Column(String)
    data = Column(JSON)
    analysis = Column(JSON, nullable=True)
    # Per-stage execution trace populated by app.core.tracing.StageTimer in
    # the Celery worker pipeline. Shape:
    #   [{stage: str, duration_ms: int, llm_calls: [{backend, duration_ms, error?}]}]
    # Self-contained inside the incident row so post-mortem doesn't need
    # a separate trip into OTel/Prometheus.
    trace = Column(JSON, nullable=True)
    user_feedback = Column(JSON, nullable=True)  # {score: 1-5, comment: str}
    is_accepted = Column(String, nullable=True)  # "ACCEPTED", "REJECTED"
    created_at = Column(DateTime, default=datetime.utcnow)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
