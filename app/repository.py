import uuid
from typing import List, Optional

import structlog
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.database import SessionLocal
from app.models import Conversation, Message, MessageRole

logger = structlog.get_logger()

# Один и тот же ответ на «нет такого диалога» и «диалог не твой». 404, а не
# 403: 403 подтвердил бы существование чужого ресурса и превратил бы ручку в
# оракул для перебора UUID-ов.
_NOT_FOUND = "Conversation not found"


def _legacy_adoption_enabled() -> bool:
    """Разрешено ли «усыновлять» диалоги без владельца (owner_sub IS NULL).

    Читаем через getattr: у app/config.py другой владелец, настройки там
    может ещё не быть. Дефолт — False (fail-closed).

    ЗАЧЕМ ВООБЩЕ ФЛАГ. После миграции 20260808_0100 у всех ранее созданных
    диалогов owner_sub IS NULL, и по консервативному правилу они становятся
    недоступны вообще никому. Флаг — аварийный вентиль на время миграционного
    окна, если оператор ГОТОВ заплатить за это ценой ниже.

    ЧЕМ ОН ОПАСЕН. Усыновление первым обратившимся — это ровно тот же IDOR,
    только одноразовый: атакующий перебирает UUID-ы и забирает legacy-диалоги
    себе (и читает их историю) быстрее, чем это сделают настоящие владельцы.
    Поэтому по умолчанию ВЫКЛЮЧЕНО, включать — только осознанно и ненадолго.
    """
    return bool(getattr(settings, "COPILOT_LEGACY_CONVERSATION_ADOPTION", False))


def _load_owned_conversation(
    session: Session,
    conv_id: uuid.UUID,
    owner_sub: Optional[str],
) -> Conversation:
    """Достаёт диалог и проверяет принадлежность `owner_sub`.

    `owner_sub=None` означает «проверку владельца не запрашивали» — это
    режим для внутренних/административных вызовов. Все пути, куда приходит
    запрос пользователя, ОБЯЗАНЫ передавать `owner_sub=user.sub`.

    Несуществующий диалог и чужой диалог неотличимы снаружи: оба → 404.

    Legacy-строки (owner_sub IS NULL) по умолчанию трактуются как ЧУЖИЕ.
    Так консервативнее: до миграции владельца не писал никто, значит
    «пусто» не равно «ничей» — за NULL может стоять чей угодно диалог, и
    отдать его первому постучавшему = оставить ту же дыру открытой ещё на
    один заход. Обратная сторона (настоящий владелец теряет свою историю)
    лечится либо новым диалогом, либо ручным backfill-ом оператора, либо
    временным COPILOT_LEGACY_CONVERSATION_ADOPTION — см.
    _legacy_adoption_enabled.
    """
    conv = session.get(Conversation, conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)

    if owner_sub is None:
        return conv

    if conv.owner_sub == owner_sub:
        return conv

    if conv.owner_sub is None and _legacy_adoption_enabled():
        # Усыновление включено явно — фиксируем в логах, это событие
        # смены владельца, а не рутина. Коммит делает вызывающий.
        conv.owner_sub = owner_sub
        logger.warning(
            "conversation_legacy_adopted",
            conversation_id=str(conv_id),
            owner_sub=owner_sub,
        )
        return conv

    logger.warning(
        "conversation_access_denied",
        conversation_id=str(conv_id),
        owner_sub=owner_sub,
        # Чужой sub наружу не светим и в лог не пишем — достаточно факта,
        # что владелец не совпал (или его нет).
        owner_present=conv.owner_sub is not None,
    )
    raise HTTPException(status_code=404, detail=_NOT_FOUND)


async def create_conversation(owner_sub: Optional[str] = None) -> uuid.UUID:
    """
    Creates a new conversation record and returns its UUID.
    Executes sync DB operation in a threadpool to keep it async-compatible.

    `owner_sub` — JWT-claim `sub` создателя. Без него диалог получится
    «ничей», то есть недоступный по обычному пути (см.
    _load_owned_conversation) — так что вызывающий из HTTP-слоя обязан его
    передавать.
    """

    def _sync_create():
        with SessionLocal() as session:
            conv = Conversation(owner_sub=owner_sub)
            session.add(conv)
            session.commit()
            return conv.id

    return await run_in_threadpool(_sync_create)


async def add_message(
    conv_id: uuid.UUID,
    role: MessageRole,
    content: str,
    owner_sub: Optional[str] = None,
) -> Message:
    """
    Adds a message to an existing conversation.
    Raises 404 if the conversation does not exist.

    При переданном `owner_sub` дописать сообщение можно только в СВОЙ диалог:
    чужой и несуществующий одинаково дают 404. Проверка и запись идут в одной
    сессии — между ними нельзя вклиниться.
    """

    def _sync_add():
        with SessionLocal() as session:
            _load_owned_conversation(session, conv_id, owner_sub)

            message = Message(conversation_id=conv_id, role=role, content=content)
            session.add(message)
            session.commit()
            session.refresh(message)
            return message

    return await run_in_threadpool(_sync_add)


async def get_conversation(
    conv_id: uuid.UUID, owner_sub: Optional[str] = None
) -> Conversation:
    """
    Retrieves a single conversation by ID with all its messages eager-loaded.
    Raises 404 if not found.

    При переданном `owner_sub` чужой диалог тоже даёт 404 — читать чужую
    переписку так же нельзя, как и дописывать в неё. Legacy-строки
    (owner_sub IS NULL) на чтении НЕ усыновляются даже при включённом
    COPILOT_LEGACY_CONVERSATION_ADOPTION: смена владельца — это запись, ей
    не место в GET-пути.
    """

    def _sync_get():
        with SessionLocal() as session:
            stmt = (
                select(Conversation)
                .options(joinedload(Conversation.messages))
                .where(Conversation.id == conv_id)
            )
            if owner_sub is not None:
                stmt = stmt.where(Conversation.owner_sub == owner_sub)
            conv = session.execute(stmt).unique().scalar_one_or_none()
            if not conv:
                raise HTTPException(status_code=404, detail=_NOT_FOUND)
            return conv

    return await run_in_threadpool(_sync_get)


async def list_conversations(
    limit: int = 20, offset: int = 0, owner_sub: Optional[str] = None
) -> List[Conversation]:
    """
    Returns a list of conversations ordered by creation date (newest first).

    При переданном `owner_sub` выдаются только диалоги этого пользователя
    (legacy-строки с NULL в выдачу не попадают — они ничьи).
    """

    def _sync_list():
        with SessionLocal() as session:
            stmt = select(Conversation)
            if owner_sub is not None:
                stmt = stmt.where(Conversation.owner_sub == owner_sub)
            stmt = (
                stmt.order_by(Conversation.created_at.desc()).limit(limit).offset(offset)
            )
            return list(session.execute(stmt).scalars().all())

    return await run_in_threadpool(_sync_list)
