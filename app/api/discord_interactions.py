"""Discord Interactions endpoint — обрабатывает нажатия кнопок 👍 / 👎.

Discord отправляет POST на этот endpoint при любом взаимодействии с компонентом.
Мы верифицируем Ed25519-подпись (требование Discord), сохраняем фидбек в БД
через существующую логику /evaluation/{id}/submit, возвращаем ephemeral-ответ.

Регистрация:
  Discord Developer Portal → Application → General Information →
  Interactions Endpoint URL = https://<your-host>/discord/interactions

Поток "защита от дурака" для 👎:
  1. feedback_neg_{id}         → ephemeral-подтверждение с двумя кнопками (НЕ сохраняет)
  2. feedback_neg_confirm_{id} → сохраняет негативный фидбек
  3. feedback_neg_cancel_{id}  → ephemeral "Отменено"
  👍 сохраняется сразу (feedback_pos_{id}).
"""
from __future__ import annotations

import binascii
import json
import logging
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.database import IncidentRecord, SessionLocal

logger = logging.getLogger(__name__)

router = APIRouter()

# Discord interaction types
_PING              = 1
_MESSAGE_COMPONENT = 3

# Discord interaction callback types
_PONG                                  = 1
_CHANNEL_MESSAGE                       = 4   # immediate visible response
_DEFERRED_CHANNEL_MESSAGE              = 5   # "thinking…" loader, followup via PATCH
_EPHEMERAL_FLAG                        = 64  # only the clicker sees it

# Discord даёт ~3s до initial response от interactions endpoint. На дольше — type=5
# (deferred) + PATCH followup в течение 15 минут. Apply-flow ≥ 3s в реальности
# на сетевых вызовах kubectl, поэтому defer обязателен.
_DISCORD_API_BASE = "https://discord.com/api/v10"

# Discord component types
_ACTION_ROW = 1
_BUTTON     = 2

# Discord button styles
_BTN_PRIMARY   = 1  # blurple
_BTN_SECONDARY = 2  # grey
_BTN_SUCCESS   = 3  # green
_BTN_DANGER    = 4  # red


def _verify_signature(public_key_hex: str, signature_hex: str, timestamp: str, body: bytes) -> bool:
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        pub.verify(bytes.fromhex(signature_hex), timestamp.encode() + body)
        return True
    except (InvalidSignature, ValueError, binascii.Error):
        return False


def _store_feedback(incident_id: str, verdict: str, discord_user_id: str) -> bool:
    """Пишет фидбек в IncidentRecord.user_feedback + is_accepted. Возвращает False если запись не найдена."""
    db: Session = SessionLocal()
    try:
        record = (
            db.query(IncidentRecord)
            .filter(IncidentRecord.incident_id == incident_id)
            .first()
        )
        if record is None:
            return False
        is_accepted = verdict == "positive"
        record.is_accepted = "ACCEPTED" if is_accepted else "REJECTED"
        record.user_feedback = {
            "score": 5 if is_accepted else 1,
            "comment": None,
            "discord_user_id": discord_user_id,
            "source": "discord_button",
        }
        db.commit()
        logger.info("discord_feedback_stored incident=%s verdict=%s user=%s",
                    incident_id, verdict, discord_user_id)
        return True
    finally:
        db.close()


def _ephemeral(content: str, components: list | None = None) -> dict:
    data: dict[str, Any] = {"content": content, "flags": _EPHEMERAL_FLAG}
    if components:
        data["components"] = components
    return {"type": _CHANNEL_MESSAGE, "data": data}


def _deferred_ephemeral() -> dict:
    """Return-payload, который говорит Discord "показывай loader, ответ придёт followup-ом"."""
    return {"type": _DEFERRED_CHANNEL_MESSAGE, "data": {"flags": _EPHEMERAL_FLAG}}


async def _send_followup(interaction_token: str, content: str) -> None:
    """PATCH @original — заменить deferred loader финальным ephemeral-сообщением.

    Доступно в течение 15 минут после initial response. Не падаем наружу —
    хуже всего получим лог-ошибку и пользователь увидит loading-бесконечно.
    """
    app_id = settings.DISCORD_APPLICATION_ID
    if not app_id:
        logger.warning("discord_followup_skipped reason=no_app_id")
        return
    import httpx
    url = f"{_DISCORD_API_BASE}/webhooks/{app_id}/{interaction_token}/messages/@original"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.patch(url, json={"content": content[:2000]})
            if r.status_code >= 400:
                logger.error(
                    "discord_followup_failed status=%s body=%s",
                    r.status_code, r.text[:200],
                )
    except Exception as e:
        logger.error("discord_followup_exception error=%s", str(e))


async def _apply_in_background(incident_id: str, user_id: str, interaction_token: str) -> None:
    """Запустить apply_intent + отправить followup-ответ.

    apply_intent — sync (subprocess), запускаем в to_thread; результат форматируем
    тем же _format_apply_result-style как раньше и PATCH в Discord.
    """
    import asyncio
    from app.services.executor_apply import apply_intent
    outcome = await asyncio.to_thread(apply_intent, incident_id, user_id)

    if not outcome["ok"]:
        content = _format_apply_refusal(incident_id, outcome.get("reason", "unknown"))
    else:
        result = outcome.get("result") or {}
        success = bool(result.get("success"))
        emoji = "✅" if success else "❌"
        command = result.get("command", "(unknown)")
        out = (result.get("stdout") or result.get("stderr") or result.get("error") or "").strip()
        content = f"{emoji} kubectl {'выполнен' if success else 'упал'}: `{command}`"
        if out:
            content += f"\n```\n{out[:600]}\n```"

    await _send_followup(interaction_token, content)


def _confirm_neg_buttons(incident_id: str) -> list:
    """Два кнопки для подтверждения негативного фидбека."""
    return [{
        "type": _ACTION_ROW,
        "components": [
            {
                "type": _BUTTON,
                "style": _BTN_DANGER,
                "label": "Да, выводы неверны",
                "custom_id": f"feedback_neg_confirm_{incident_id}",
            },
            {
                "type": _BUTTON,
                "style": _BTN_SECONDARY,
                "label": "Отмена",
                "custom_id": f"feedback_neg_cancel_{incident_id}",
            },
        ],
    }]


def _confirm_apply_buttons(incident_id: str) -> list:
    """Двухшаговое подтверждение запуска kubectl. Защита от случайного клика."""
    return [{
        "type": _ACTION_ROW,
        "components": [
            {
                "type": _BUTTON,
                "style": _BTN_DANGER,
                "label": "Да, запустить kubectl",
                "custom_id": f"apply_confirm_{incident_id}",
            },
            {
                "type": _BUTTON,
                "style": _BTN_SECONDARY,
                "label": "Отмена",
                "custom_id": f"apply_cancel_{incident_id}",
            },
        ],
    }]


_APPLY_REFUSAL_MESSAGES = {
    "incident_not_found": "❌ Инцидент `{incident_id}` не найден.",
    "no_intent":          "❌ Нет ExecutionIntent для применения — пайплайн не выдал структурный фикс.",
    "already_applied":    "ℹ️ Уже применено ранее — действие идемпотентно.",
    "dry_run_not_ok":     "❌ dry-run не прошёл — реальный запуск заблокирован.",
}


def _format_apply_refusal(incident_id: str, reason: str) -> str:
    """Подобрать человекочитаемое сообщение для отказа apply-flow."""
    # Ключ может быть составной: "risk_too_high:high", "dry_run_not_ok:guardrail_blocked" и т.п.
    key = reason.split(":", 1)[0]
    if key in _APPLY_REFUSAL_MESSAGES:
        return _APPLY_REFUSAL_MESSAGES[key].format(incident_id=incident_id)
    if key == "risk_too_high":
        return "❌ Action risk=`high` — auto-apply запрещён, действуй вручную."
    if key == "intent_invalid":
        return "❌ ExecutionIntent невалиден после round-trip — manual triage."
    return f"❌ Не могу применить: {reason}"


@router.post("/interactions")
async def discord_interactions(
    request: Request,
    x_signature_ed25519: str = Header(..., alias="X-Signature-Ed25519"),
    x_signature_timestamp: str = Header(..., alias="X-Signature-Timestamp"),
) -> Any:
    body = await request.body()

    if not settings.DISCORD_PUBLIC_KEY:
        raise HTTPException(status_code=503, detail="DISCORD_PUBLIC_KEY not configured")

    if not _verify_signature(
        settings.DISCORD_PUBLIC_KEY,
        x_signature_ed25519,
        x_signature_timestamp,
        body,
    ):
        raise HTTPException(status_code=401, detail="Invalid Discord signature")

    payload = json.loads(body)
    interaction_type = payload.get("type")

    # Discord требует ответить на PING, чтобы подтвердить ownership endpoint-а.
    if interaction_type == _PING:
        return {"type": _PONG}

    if interaction_type != _MESSAGE_COMPONENT:
        # Прочие типы — Discord не требует специфичного ответа.
        return {"type": _PONG}

    custom_id: str = payload.get("data", {}).get("custom_id", "")
    user_id: str = (
        payload.get("member", {}).get("user", {}).get("id", "")
        or payload.get("user", {}).get("id", "")
    )

    # ── 👍 Позитивный фидбек — сохраняем сразу ──────────────────────────────
    if custom_id.startswith("feedback_pos_"):
        incident_id = custom_id[len("feedback_pos_"):]
        found = _store_feedback(incident_id, "positive", user_id)
        if not found:
            return _ephemeral(f"Инцидент `{incident_id}` не найден в БД.")
        return _ephemeral("✅ Отмечено как **верное решение**. Спасибо!")

    # ── 👎 Шаг 1: запрашиваем подтверждение, ничего не сохраняем ────────────
    if custom_id.startswith("feedback_neg_") and not custom_id.startswith("feedback_neg_confirm_") and not custom_id.startswith("feedback_neg_cancel_"):
        incident_id = custom_id[len("feedback_neg_"):]
        return _ephemeral(
            "⚠️ Подтверди: **выводы модели были ошибочными**?\n"
            "-# (не сам алерт, а анализ причины и рекомендации)",
            components=_confirm_neg_buttons(incident_id),
        )

    # ── 👎 Шаг 2: подтверждение — сохраняем негативный фидбек ───────────────
    if custom_id.startswith("feedback_neg_confirm_"):
        incident_id = custom_id[len("feedback_neg_confirm_"):]
        found = _store_feedback(incident_id, "negative", user_id)
        if not found:
            return _ephemeral(f"Инцидент `{incident_id}` не найден в БД.")
        return _ephemeral(
            "👎 Зафиксировано как **ошибочный анализ**. "
            "Для разбора — см. GitLab PR или k8s-логи."
        )

    # ── 👎 Отмена подтверждения ──────────────────────────────────────────────
    if custom_id.startswith("feedback_neg_cancel_"):
        return _ephemeral("Отменено.")

    # ── ⚙️ Apply: шаг 1, ephemeral-подтверждение ────────────────────────────
    if custom_id.startswith("apply_") and not custom_id.startswith("apply_confirm_") and not custom_id.startswith("apply_cancel_"):
        incident_id = custom_id[len("apply_"):]
        if not settings.EXECUTOR_APPROVAL_ENABLED:
            return _ephemeral("❌ EXECUTOR_APPROVAL_ENABLED=false — apply отключён.")
        return _ephemeral(
            "⚠️ Запустить **kubectl** для этой команды? "
            "dry-run уже прошёл (kube-apiserver валидировал команду), "
            "сейчас будет реальный write.\n"
            "-# Действие записывается в audit + OTEL.",
            components=_confirm_apply_buttons(incident_id),
        )

    # ── ⚙️ Apply: шаг 2, выполнить kubectl (deferred response) ──────────────
    if custom_id.startswith("apply_confirm_"):
        incident_id = custom_id[len("apply_confirm_"):]
        if not settings.EXECUTOR_APPROVAL_ENABLED:
            return _ephemeral("❌ EXECUTOR_APPROVAL_ENABLED=false — apply отключён.")

        interaction_token = payload.get("token", "")
        if not interaction_token:
            # Без token-а не сможем сделать followup; fallback на sync-режим.
            return _ephemeral("❌ Discord interaction token отсутствует — apply не запущен.")

        # Сразу возвращаем "thinking..." (type=5), apply работает в background-task.
        # Discord даёт 15 минут на followup через PATCH @original — этого хватит
        # даже для большого rollout restart с тяжёлыми initContainers.
        import asyncio
        asyncio.create_task(_apply_in_background(incident_id, user_id, interaction_token))
        return _deferred_ephemeral()

    # ── ⚙️ Apply: отмена ───────────────────────────────────────────────────
    if custom_id.startswith("apply_cancel_"):
        return _ephemeral("Apply отменён.")

    return _ephemeral("Неизвестное действие.")
