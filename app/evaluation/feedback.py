from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import User, get_current_user
from app.database import IncidentRecord, get_db

router = APIRouter()


class FeedbackSubmit(BaseModel):
    score: int = Field(ge=1, le=5)  # 1 to 5
    is_accepted: bool
    comment: Optional[str] = Field(default=None, max_length=2000)


@router.post("/{incident_id}/submit")
async def submit_feedback(
    incident_id: str,
    feedback: FeedbackSubmit,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Эндпоинт для сбора обратной связи от инженеров по результатам RCA.
    """
    record = (
        db.query(IncidentRecord)
        .filter(IncidentRecord.incident_id == incident_id)
        .first()
    )

    if not record:
        raise HTTPException(status_code=404, detail="Incident record not found")

    # Привязываем автора фидбэка из JWT (sub).
    record.user_feedback = {
        "score": feedback.score,
        "comment": feedback.comment,
        "author": user.sub,
    }
    record.is_accepted = "ACCEPTED" if feedback.is_accepted else "REJECTED"

    db.commit()
    return {
        "status": "recorded",
        "incident_id": incident_id,
        "is_accepted": record.is_accepted,
    }


@router.get("/stats")
async def get_governance_stats(db: Session = Depends(get_db)):
    """
    Возвращает агрегированную статистику точности ИИ.
    """
    total = (
        db.query(IncidentRecord).filter(IncidentRecord.is_accepted.isnot(None)).count()
    )
    accepted = (
        db.query(IncidentRecord)
        .filter(IncidentRecord.is_accepted == "ACCEPTED")
        .count()
    )

    accuracy = (accepted / total * 100) if total > 0 else 0

    return {
        "total_evaluated_incidents": total,
        "accepted_count": accepted,
        "accuracy_rate": f"{accuracy:.2f}%",
    }
