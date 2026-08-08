import enum
import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import UUID, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for SQLAlchemy models."""


class MessageRole(str, enum.Enum):
    user = "user"
    assistant = "assistant"


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Владелец диалога — JWT-claim `sub` (см. app/auth.py: User.sub). До
    # 08.08.2026 у Conversation владельца не было ВООБЩЕ, и /copilot принимал
    # любой conversation_id от любого аутентифицированного пользователя: чужой
    # диалог можно было дописать, сдвинуть его state machine и прочитать ответ
    # через /jobs/{task_id} (горизонтальная запись, IDOR).
    #
    # nullable=True — у строк, созданных ДО миграции 20260808_0100, владельца
    # нет и взять его неоткуда. Такие legacy-строки трактуются как ЧУЖИЕ
    # (404), см. app/repository.py::_load_owned_conversation.
    #
    # index=True — по owner_sub фильтруются выборки диалогов пользователя
    # (list_conversations). Имя индекса, которое SQLAlchemy выводит по
    # умолчанию, — `ix_conversations_owner_sub`; ровно оно создаётся в
    # миграции, чтобы не было дрейфа модель↔схема.
    owner_sub: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, index=True
    )
    current_state: Mapped[str] = mapped_column(String, default="OPEN")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    messages: Mapped[List["Message"]] = relationship(
        "Message", back_populates="conversation", cascade="all, delete-orphan"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id"), nullable=False
    )
    role: Mapped[MessageRole] = mapped_column(Enum(MessageRole), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    conversation: Mapped["Conversation"] = relationship(
        "Conversation", back_populates="messages"
    )
