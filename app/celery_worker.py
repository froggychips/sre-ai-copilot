import asyncio
import structlog
from celery import Celery
from openai import AsyncOpenAI
from app.config import settings
from app import repository
from app.models import MessageRole

# Initialize structured logger
logger = structlog.get_logger()

# Initialize Celery instance
celery_app = Celery(
    'worker',
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL
)

# Celery configuration for production
celery_app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    task_track_started=True,
    task_time_limit=300,  # 5 minutes timeout
)

async def _generate_reply_logic(conversation_id: str, prompt: str) -> str:
    """
    Internal async logic to handle OpenAI call and database persistence.
    """
    # Initialize modern OpenAI async client
    client = AsyncOpenAI(api_key=settings.GEMINI_API_KEY) # Using GEMINI_API_KEY as generic placeholder for OpenAI key if needed, or update config
    
    log = logger.bind(conversation_id=conversation_id)
    
    try:
        log.info("generating_llm_reply", model=settings.MODEL_NAME)
        
        # 1. Call OpenAI API (or Gemini in this context)
        response = await client.chat.completions.create(
            model=settings.MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=settings.MAX_TOKENS,
        )
        
        assistant_message = response.choices[0].message.content
        
        # 2. Persist the assistant response in PostgreSQL
        await repository.add_message(
            conv_id=conversation_id,
            role=MessageRole.assistant,
            content=assistant_message
        )
        
        log.info("reply_persisted_successfully")
        return assistant_message

    except Exception as e:
        log.error("celery_task_failed", error=str(e))
        raise

@celery_app.task(name='generate_reply', bind=True, max_retries=3)
def generate_reply(self, conversation_id: str, prompt: str):
    """
    Celery task wrapper that executes async LLM logic using asyncio.run.
    """
    try:
        return asyncio.run(_generate_reply_logic(conversation_id, prompt))
    except Exception as exc:
        # Exponential backoff for retries on transient failures
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)
