import os
import sys
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context

# Гарантируем что родительская директория (где лежит package `app`) в sys.path.
# Без этого `alembic upgrade head` в pod-е падает с ModuleNotFoundError.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Import the models and settings.
# В проекте два независимых declarative Base:
#   app.models.Base      — conversations / messages
#   app.database.Base    — IncidentRecord + kg_* + discord_dedup
#
# ВАЖНО: сам по себе импорт `app.database.Base` даёт ПУСТУЮ metadata по kg_*.
# Модели регистрируются в Base.metadata только при импорте модулей, где они
# объявлены. Без импортов ниже autogenerate видел лишь
# ['conversations', 'messages', 'incidents'] и на любой `alembic revision
# --autogenerate` генерил drop_table() на все ~17 kg_*-таблиц (data-loss).
# Поэтому явно импортируем ВСЕ модули с ORM-моделями (side-effect-импорты):
from app.models import Base as ModelsBase
from app.database import Base as DatabaseBase
import app.knowledge_graph.schema  # noqa: F401 — kg_services, kg_alerts, ... (14 таблиц)
import app.remediation.models  # noqa: F401 — kg_remediation_decisions
import app.services.discord.dedup_store  # noqa: F401 — discord_dedup
from app.config import settings

# Guard от регрессии: если кто-то вынесет модели так, что side-effect-импорты
# выше перестанут их регистрировать — падаем сразу, а не молча генерим drop'ы.
_visible = set(ModelsBase.metadata.tables) | set(DatabaseBase.metadata.tables)
for _required in ("conversations", "kg_services", "kg_service_edges", "discord_dedup"):
    assert _required in _visible, (
        f"alembic/env.py: таблица {_required!r} не видна в target_metadata — "
        "проверь side-effect-импорты моделей выше (риск ложных drop_table в autogenerate)"
    )

# This is the Alembic Config object, which provides access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set target metadata for autogenerate (оба Base — см. комментарий к импортам).
target_metadata = [ModelsBase.metadata, DatabaseBase.metadata]

def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = settings.DATABASE_URL
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    # Override sqlalchemy.url with value from settings
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = settings.DATABASE_URL
    
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
