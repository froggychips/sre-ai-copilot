from app.database import SessionLocal, IncidentRecord
from app.evaluation.metrics import track_result, track_feedback
from fastapi import APIRouter

router = APIRouter()

@router.post("/feedback/{incident_id}")
async def submit_feedback(incident_id: int, score: int, accepted: bool):
    """
    Принимает оценку от инженера: Accepted/Rejected и рейтинг (1-5).
    """
    db = SessionLocal()
    # Сохраняем в БД для истории обучения
    track_result(accepted)
    track_feedback(score)
    db.close()
    return {"status": "feedback_recorded"}
