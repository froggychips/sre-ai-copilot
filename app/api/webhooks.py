from fastapi import APIRouter, Request, BackgroundTasks
from app.workers.tasks import q, start_worker_task
import logging

router = APIRouter()

@router.post("/newrelic")
async def newrelic_webhook(request: Request):
    data = await request.json()
    logging.info(f"Received New Relic webhook for incident: {data.get('incident_id')}")
    
    # Queue the incident for background processing
    q.enqueue(start_worker_task, data)
    
    return {"status": "accepted", "incident_id": data.get("incident_id")}

@router.get("/health")
async def health_check():
    return {"status": "healthy"}
