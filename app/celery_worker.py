import asyncio
import structlog
from celery import Celery
from openai import AsyncOpenAI
from app.config import settings
from app import repository
from app.models import MessageRole, Conversation
from app.database import SessionLocal
from app.core.state_machine import StateMachine, IncidentState
from app.services.resilience import LLMResilienceManager
from app.services.session_manager import SessionManager
from redis.asyncio import from_url

# Initialize services
logger = structlog.get_logger()
redis_client = from_url(settings.REDIS_URL)
resilience = LLMResilienceManager(redis_client)
session_manager = SessionManager(redis_client)

celery_app = Celery(
    'worker',
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL
)

celery_app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    task_track_started=True,
    task_time_limit=300,
)

async def _generate_reply_logic(conversation_id: str, prompt: str) -> str:
    db = SessionLocal()
    conv = db.query(Conversation).filter_by(id=conversation_id).first()
    
    def transition(to_state: IncidentState):
        if not StateMachine.validate_transition(IncidentState(conv.current_state), to_state):
            raise Exception(f"Invalid transition from {conv.current_state} to {to_state}")
        conv.current_state = to_state.value
        db.commit()

    try:
        transition(IncidentState.INVESTIGATING)
        
        client = AsyncOpenAI(api_key=settings.GEMINI_API_KEY)
        response = await client.chat.completions.create(
            model=settings.MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=settings.MAX_TOKENS,
        )
        assistant_message = response.choices[0].message.content
        
        transition(IncidentState.HYPOTHESIS_GENERATED)
        
        await repository.add_message(conversation_id, MessageRole.assistant, assistant_message)
        
        transition(IncidentState.FIX_PROPOSED)
        return assistant_message

    except Exception as e:
        if conv:
            conv.current_state = IncidentState.FAILED.value
            db.commit()
        logger.error("celery_task_failed", error=str(e))
        raise
    finally:
        db.close()

@celery_app.task(name='generate_reply', bind=True, max_retries=3)
def generate_reply(self, conversation_id: str, prompt: str):
    try:
        return asyncio.run(_generate_reply_logic(conversation_id, prompt))
    except Exception as exc:
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)
