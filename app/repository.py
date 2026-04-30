import uuid
from typing import List
from starlette.concurrency import run_in_threadpool
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from app.db.session import SessionLocal
from app.models import Conversation, Message, MessageRole


async def create_conversation() -> uuid.UUID:
    """
    Creates a new conversation record and returns its UUID.
    Executes sync DB operation in a threadpool to keep it async-compatible.
    """
    def _sync_create():
        with SessionLocal() as session:
            conv = Conversation()
            session.add(conv)
            session.commit()
            return conv.id
            
    return await run_in_threadpool(_sync_create)


async def add_message(conv_id: uuid.UUID, role: MessageRole, content: str) -> Message:
    """
    Adds a message to an existing conversation. 
    Raises 404 if the conversation does not exist.
    """
    def _sync_add():
        with SessionLocal() as session:
            # Verify conversation existence
            conv = session.get(Conversation, conv_id)
            if not conv:
                raise HTTPException(status_code=404, detail="Conversation not found")
            
            message = Message(
                conversation_id=conv_id,
                role=role,
                content=content
            )
            session.add(message)
            session.commit()
            session.refresh(message)
            return message
            
    return await run_in_threadpool(_sync_add)


async def get_conversation(conv_id: uuid.UUID) -> Conversation:
    """
    Retrieves a single conversation by ID with all its messages eager-loaded.
    Raises 404 if not found.
    """
    def _sync_get():
        with SessionLocal() as session:
            stmt = (
                select(Conversation)
                .options(joinedload(Conversation.messages))
                .where(Conversation.id == conv_id)
            )
            conv = session.execute(stmt).unique().scalar_one_or_none()
            if not conv:
                raise HTTPException(status_code=404, detail="Conversation not found")
            return conv
            
    return await run_in_threadpool(_sync_get)


async def list_conversations(limit: int = 20, offset: int = 0) -> List[Conversation]:
    """
    Returns a list of conversations ordered by creation date (newest first).
    """
    def _sync_list():
        with SessionLocal() as session:
            stmt = (
                select(Conversation)
                .order_by(Conversation.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            return list(session.execute(stmt).scalars().all())
            
    return await run_in_threadpool(_sync_list)
