import asyncio
import json
import structlog
from celery import Celery
from app.config import settings
from app import repository
from app.models import MessageRole, Conversation
from app.database import SessionLocal
from app.core.state_machine import StateMachine, IncidentState
from app.context.context_builder import ContextBuilder
from app.agents.analyzer import AnalyzerAgent
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

async def _generate_reply_logic(conversation_id: str, prompt: str, replay_mode: bool = False) -> str:
    db = SessionLocal()
    conv = db.query(Conversation).filter_by(id=conversation_id).first()
    
    def transition(to_state: IncidentState):
        if not StateMachine.validate_transition(IncidentState(conv.current_state), to_state):
            # В режиме replay игнорируем ошибки переходов для гибкости
            if not replay_mode:
                raise Exception(f"Invalid transition from {conv.current_state} to {to_state}")
        conv.current_state = to_state.value
        db.commit()

    try:
        transition(IncidentState.INVESTIGATING)
        builder = ContextBuilder()
        enriched_ctx = builder.build_context(conv.data)
        
        # Reasoning Loop
        for iteration in range(3):
            analyzer = AnalyzerAgent()
            analysis_data = await analyzer.analyze(enriched_ctx)
            
            try:
                analysis = json.loads(analysis_data)
                confidence = analysis.get("confidence_score", 0)
            except:
                confidence = 0
            
            if confidence >= 0.7:
                transition(IncidentState.HYPOTHESIS_GENERATED)
                break
            else:
                logger.warning("low_confidence_loop", iteration=iteration, score=confidence)
                enriched_ctx["socratic_feedback"] = "Confidence too low. Focus on specific pod logs."
        else:
            raise Exception("Failed to reach confidence threshold after 3 iterations")
        
        transition(IncidentState.FIX_PROPOSED)
        
        # В режиме REPLAY пропускаем отправку в Discord
        if not replay_mode:
            from app.services.discord_service import discord_service
            await discord_service.send_report(f"Analysis for incident {conversation_id} completed.")
            
        return analysis_data

    except Exception as e:
        if conv:
            conv.current_state = IncidentState.FAILED.value
            db.commit()
        logger.error("celery_task_failed", error=str(e))
        raise
    finally:
        db.close()

@celery_app.task(name='generate_reply', bind=True, max_retries=3)
def generate_reply(self, conversation_id: str, prompt: str, replay_mode: bool = False):
    try:
        return asyncio.run(_generate_reply_logic(conversation_id, prompt, replay_mode))
    except Exception as exc:
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)
