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
    # (pool_size=5/max_overflow=10 → потолок 15) давал `QueuePool limit ...
    # timed out` на api.
    #
    # ВАЖНО про потолок: engine создаётся В КАЖДОМ процессе. Процессов ~13:
    # 2 api-pod-а + 2 worker-pod-а × (parent + 4 prefork-детей) + beat +
    # migrate/cron. Прежние 20+20=40 на процесс давали теоретический потолок
    # 13×40=520 коннектов при дефолтном max_connections=100 у Postgres —
    # шторм «FATAL: sorry, too many clients already» на ровном месте.
    # 10+20=30 держит api-burst (threadpool ~40 редко весь в БД одновременно,
    # pool_timeout=30 сглаживает пики), а worker-дети реально держат 1-2
    # коннекта. Требование к серверу: Postgres max_connections >= 200
    # (см. k8s/postgres.yaml) либо pgbouncer перед БД.
    # TODO(config): вынести pool_size/max_overflow в app/config.py, чтобы
    # api и worker можно было сайзить раздельно (у config.py другой владелец).
    _pool_kwargs = {
        "pool_size": 10,
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


# Celery prefork: ребёнок наследует engine родителя вместе с уже открытыми
# сокетами Postgres. Два процесса, пишущие в один сокет, дают «weird errors»
# класса lost synchronization with server / SSL decryption failed.
# Рекомендация SQLAlchemy для fork: в каждом ребёнке сразу выбросить
# унаследованный пул (close=False — сокеты родителя из ребёнка НЕ закрываем,
# ими продолжает пользоваться родитель) и дать ребёнку набрать свои коннекты.
try:
    from celery.signals import worker_process_init

    @worker_process_init.connect
    def _dispose_engine_after_fork(**_kwargs) -> None:
        engine.dispose(close=False)
except ImportError:  # окружение без celery (например, часть скриптов)
    pass


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
