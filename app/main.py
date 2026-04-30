from fastapi import FastAPI, Request
from app.api import webhooks
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import logging

logging.basicConfig(level=logging.INFO)

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="SRE AI Copilot", version="1.0.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])

@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    # Basic access logging for audit
    logging.info(f"Access: {request.method} {request.url.path} from {request.client.host}")
    return await call_next(request)

@app.get("/")
async def root():
    return {"message": "SRE AI Copilot is running"}
