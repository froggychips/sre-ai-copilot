import uuid
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
import structlog

class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    FastAPI middleware to generate/propagate X-Request-ID and 
    bind it to the structlog context for the duration of the request.
    """
    async def dispatch(self, request: Request, call_next) -> Response:
        # Get request ID from headers or generate new one
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        
        # Clear previous context and bind current request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        
        response: Response = await call_next(request)
        
        # Propagate request ID in response headers
        response.headers["X-Request-ID"] = request_id
        return response
