from sqlalchemy.orm import sessionmaker
from app.db.engine import engine

# Configured sessionmaker for SQLAlchemy 2.0
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)
