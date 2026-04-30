from fastapi import APIRouter, Request, BackgroundTasks, HTTPException
from app.workers.tasks import q, start_worker_task
from app.config import settings
from slowapi import Limiter
from slowapi.util import get_remote_address
import logging

router = APIRouter()
limiter = Limiter(key_func=get_remote_address)

async def verify_signature(request: Request):
    if not settings.NEW_RELIC_WEBHOOK_SECRET:
        return True # Dev mode
    
    header_secret = request.headers.get("X-NewRelic-Secret")
    return header_secret == settings.NEW_RELIC_WEBHOOK_SECRET

@router.post("/newrelic")
@limiter.limit("5/minute")
async def newrelic_webhook(request: Request):
    if not await verify_signature(request):
        raise HTTPException(status_code=403, detail="Invalid signature")
        
    data = await request.json()
    incident_id = data.get("incident_id")
    logging.info(f"Accepted incident: {incident_id}")
    
    # Queue for async processing
    q.enqueue(start_worker_task, data)
    
    return {"status": "accepted", "incident_id": incident_id}

@router.get("/health")
async def health_check():
    return {"status": "healthy"}
