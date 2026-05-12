from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, Integer, String, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from app.config import settings

engine = create_engine(settings.DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
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
