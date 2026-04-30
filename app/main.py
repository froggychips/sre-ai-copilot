from fastapi import FastAPI
from app.api import webhooks
import logging

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="SRE AI Copilot", version="1.0.0")

app.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])

@app.get("/")
async def root():
    return {"message": "SRE AI Copilot is running"}
