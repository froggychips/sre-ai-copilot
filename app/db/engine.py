from sqlalchemy import create_engine
from app.config import settings

# SQLAlchemy 2.0 engine configuration
engine = create_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
)
