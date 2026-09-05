"""Единый источник engine / SessionLocal / Base для всего приложения.

Все модули обязаны импортировать отсюда. Параллельные обёртки в app/db/*
удалены — наличие двух SessionLocal вело к утечкам соединений и race
между Celery worker-ом и FastAPI handler-ом.

ИНВАРИАНТ ВРЕМЕНИ (обязателен к соблюдению во всём приложении)
--------------------------------------------------------------
1. В БД лежит NAIVE UTC. Все DateTime-колонки — TIMESTAMP WITHOUT TIME
   ZONE, python-side default'ы — `datetime.utcnow()` (naive, значение в UTC).
   Смещения в колонке нет, поэтому «какая это зона» знает только код.
2. Сессия принудительно в UTC — `-c timezone=UTC` в connect_args ниже.
   Это то, что делает п.1 непротиворечивым: raw-SQL окна вида
   `WHERE ts > NOW() - INTERVAL '24 hours'` (app/services/stats_digest.py,
   app/knowledge_graph/alerts_resolve_sync.py — десятки мест) сравнивают
   `NOW()` (timestamptz) с naive-колонкой. PG в таком сравнении приводит
   timestamptz к timestamp ПО ТАЙМЗОНЕ СЕССИИ. При TimeZone=UTC результат
   совпадает с тем, что писал `utcnow()`; при любой другой (а PG берёт её
   из своего postgresql.conf / ALTER ROLE / переменной PGTZ окружения пода —
   т.е. извне приложения) все временные окна МОЛЧА съезжают на offset:
   дайджест за «последние 24 часа» показывает не те сутки, а
   alerts_resolve_sync недорезолвливает алерты. Без ошибок и без алертов.
3. Смешивать aware-datetime с этими колонками нельзя: сравнение
   naive и aware в Python бросает TypeError, а запись aware в
   TIMESTAMP WITHOUT TIME ZONE просто отбросит tzinfo, дав сдвиг.
   Новый код на границе (API/JSON/внешние клиенты) конвертирует явно:
   `dt.astimezone(timezone.utc).replace(tzinfo=None)` перед записью.

Полная формулировка — docs/SEMANTIC_CONTRACT.md §10 «Time / Timezone
Contract». Guard'ы — tests/test_db_engine_pool_config.py и
tests/test_idle_transaction_guard.py.
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

# Потолок на ПРОСТАИВАЮЩУЮ транзакцию. Бьёт только по `idle in transaction`
# (соединение внутри транзакции, но запрос не выполняется) — активные
# запросы, даже многоминутные синки, не затрагиваются.
#
# Зачем: таск открывает сессию, читает БД, а потом уходит в долгий ВНЕШНИЙ
# вызов — kubectl (до 180с, см. k8s_topology_resources_sync) или HTTP в
# Discord. Транзакция всё это время висит и держит ACCESS SHARE на
# прочитанных таблицах. Замер на проде 08.08.2026: шесть таких транзакций,
# старшей 25 минут. Последствия:
#   * DDL не может взять ACCESS EXCLUSIVE — миграция kg_services встала в
#     очередь и заблокировала 7 читателей, повесив приложение на 6 минут;
#   * писатели ждут друг друга, запросы копятся, event loop API не успевает
#     ответить на liveness-пробу за 5с → kubelet убивает под.
# Убитая по таймауту транзакция даёт понятную ошибку в одном таске вместо
# каскадной блокировки всей БД.
_IDLE_TX_TIMEOUT_MS = 120_000


def _build_pool_kwargs(database_url: str, cfg=settings) -> dict:
    """Kwargs пула для create_engine из Settings (cfg подменяем в тестах).

    Под нагрузкой sync-сессии гоняются через run_in_threadpool (Starlette,
    до ~40 потоков) + Celery beat-задачи + dedup_store. Дефолтный QueuePool
    (pool_size=5/max_overflow=10 → потолок 15) давал `QueuePool limit ...
    timed out` на api.

    ВАЖНО про потолок: engine создаётся В КАЖДОМ процессе. Процессов ~13:
    2 api-pod-а + 2 worker-pod-а × (parent + 4 prefork-детей) + beat +
    migrate/cron. Прежние 20+20=40 на процесс давали теоретический потолок
    13×40=520 коннектов при дефолтном max_connections=100 у Postgres —
    шторм «FATAL: sorry, too many clients already» на ровном месте.
    Дефолт 10+20=30 держит api-burst (threadpool ~40 редко весь в БД
    одновременно, pool_timeout=30 сглаживает пики), а worker-дети реально
    держат 1-2 коннекта. Значения берутся из DB_POOL_SIZE/DB_MAX_OVERFLOW
    (app/config.py) — api и worker сайзятся раздельно через env деплойментов.
    Требование к серверу: Postgres max_connections >= 200
    (см. k8s/postgres.yaml) либо pgbouncer перед БД.
    """
    if database_url.startswith("sqlite"):
        return {}
    return {
        "pool_size": cfg.DB_POOL_SIZE,
        "max_overflow": cfg.DB_MAX_OVERFLOW,
        "pool_timeout": 30,
        # Профилактически пересоздаём коннекты раз в 30 мин — k8s LB/PG
        # могут молча рвать долгоживущие idle-соединения.
        "pool_recycle": 1800,
        "connect_args": {
            # options прокидываются в libpq при установлении соединения.
            # timezone=UTC — не косметика, а несущий инвариант (см. docstring
            # модуля): в колонках naive UTC, а raw-SQL окна сравнивают их с
            # `NOW()` (timestamptz). PG приводит timestamptz к timestamp по
            # таймзоне СЕССИИ, а её дефолт приходит извне приложения
            # (postgresql.conf / ALTER ROLE / PGTZ). Пришпиливаем на стороне
            # клиента, чтобы инстанс с не-UTC зоной не сдвинул молча все окна.
            "options": (
                f"-c idle_in_transaction_session_timeout={_IDLE_TX_TIMEOUT_MS}"
                " -c timezone=UTC"
            ),
            # Потолок на УСТАНОВЛЕНИЕ соединения. Без него зависший PG
            # (fsync-recovery, сетевой blackhole) держит новый коннект до
            # TCP-таймаута ОС (минуты) — усилитель инцидента 08.08.2026:
            # /readyz ждал коннект из пула, kubelet убивал под.
            "connect_timeout": cfg.DB_CONNECT_TIMEOUT,
        },
    }


_pool_kwargs: dict = _build_pool_kwargs(settings.DATABASE_URL)

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

# Сессия для read-only аналитических тасков (дайджесты). Их SQL-чтения
# перемежаются минутами внешнего I/O (сотни VM-запросов, Discord), и обычная
# сессия всё это время висела бы в `idle in transaction` — PG убивает такие
# соединения через 120с (см. _IDLE_TX_TIMEOUT_MS выше): daily_stats_digest
# умирал на ~170-й секунде с «server closed the connection unexpectedly»
# два дня подряд (08-10.08.2026). AUTOCOMMIT закрывает транзакцию после
# каждого statement: висеть нечему, ACCESS SHARE-локи не копятся, DDL не
# блокируется. Писать в PG через эту сессию нельзя — только чтение.
ReadOnlyAutocommitSession = sessionmaker(
    bind=engine.execution_options(isolation_level="AUTOCOMMIT"),
    autocommit=False,
    autoflush=False,
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
    """Инцидент: входные данные, разбор и состояние обработки.

    `analysis` — не просто результат разбора, а ещё и машина состояний:
    в нём живут `executor_applied`, `executor_in_flight` (claim исполнителя),
    `executor_state_unknown`, `report_pending`, `report_sent`, `report_failed`
    и другие. Это осознанный компромисс ранней стадии, у которого есть цена:
    состояния не типизированы, а поиск по ним возможен только через GIN
    (миграция 20260819_0100 перевела колонки json → jsonb ради этого).

    Следующим шагом такие состояния стоит вынести в отдельные колонки —
    тогда их станет видно в схеме, а не только в коде, который их пишет.
    """

    __tablename__ = "incidents"
    id = Column(Integer, primary_key=True)
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

    # --- состояние обработки (миграция 20260819_0200) --------------------
    #
    # Вынесено из `analysis` потому, что это координация, а не данные: по
    # `report_state` решают, кому досылать отчёт, по `executor_state` — не
    # выполняется ли действие прямо сейчас в соседнем воркере. И то и другое
    # нужно искать (индекс) и обновлять без read-modify-write по JSON.
    #
    # Payload остался в `analysis`: поля embed и результат исполнения — это
    # данные, и там им место. Линия раздела — «по чему координируются»
    # против «что показываем».
    #
    # NULL значит «стадии не было», и это не то же самое, что «была и
    # завершилась». В JSON различение давало наличие ключа.
    report_state = Column(String, nullable=True, index=True)      # pending|sent|failed
    report_attempts = Column(Integer, nullable=True)
    report_updated_at = Column(DateTime, nullable=True)
    executor_state = Column(String, nullable=True, index=True)    # in_flight|applied|…
    executor_claimed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
