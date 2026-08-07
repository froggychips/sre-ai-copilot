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

import asyncio
import binascii
import json
import logging
import threading
import time
from collections import deque
from typing import Any, Coroutine, Deque, Dict, Set, Tuple

from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import IncidentRecord, SessionLocal
from app.security.replay import is_timestamp_fresh
from app.services.audit_logger import audit_service

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


# ─── Approve/Decline authorization (security hardening, PR #12) ─────────────
# Whitelist resolved at request-time from settings (so tests can monkeypatch
# settings.DISCORD_APPROVERS_USER_IDS without re-importing the module).

def _parse_csv_ids(raw: str) -> Set[str]:
    """Parse a CSV string of Discord IDs into a set, dropping blanks."""
    if not raw:
        return set()
    return {p.strip() for p in raw.split(",") if p.strip()}


def _is_authorized_approver(payload: dict) -> Tuple[bool, str]:
    """Check whether the user clicking approve/decline is whitelisted.

    Returns (allowed, reason). `reason` is one of:
      - "ok_user_whitelist"    — user_id matched DISCORD_APPROVERS_USER_IDS
      - "ok_role_whitelist"    — at least one of member.roles matched DISCORD_APPROVERS_ROLE_IDS
      - "no_approvers_configured" — both lists empty → fail-closed
      - "not_in_whitelist"     — neither user_id nor any role matched

    Fail-closed: empty config denies all clicks.
    """
    allowed_users = _parse_csv_ids(getattr(settings, "DISCORD_APPROVERS_USER_IDS", "") or "")
    allowed_roles = _parse_csv_ids(getattr(settings, "DISCORD_APPROVERS_ROLE_IDS", "") or "")

    if not allowed_users and not allowed_roles:
        return False, "no_approvers_configured"

    user_id = str(
        (payload.get("member") or {}).get("user", {}).get("id")
        or payload.get("user", {}).get("id")
        or ""
    )
    if user_id and user_id in allowed_users:
        return True, "ok_user_whitelist"

    member = payload.get("member") or {}
    member_roles = {str(r) for r in (member.get("roles") or [])}
    if member_roles & allowed_roles:
        return True, "ok_role_whitelist"

    return False, "not_in_whitelist"


# In-memory per-user rate-limit on approve/decline clicks. This is a soft
# guardrail: API + worker run as multiple processes, so the cap is per-process,
# not global. Strict global rate-limit would need Redis or DB-backed counters.
_RATE_WINDOW_SEC = 3600
_rate_state: Dict[str, Deque[float]] = {}
_rate_lock = threading.Lock()


def _check_rate_limit(user_id: str) -> bool:
    """Return True if user may click, False if over cap. Records the click on True.

    Cap from settings.DISCORD_APPROVAL_RATE_LIMIT_PER_HOUR (default 5/h).
    """
    cap = int(getattr(settings, "DISCORD_APPROVAL_RATE_LIMIT_PER_HOUR", 5) or 0)
    if cap <= 0:
        # 0 / negative disables the limiter.
        return True
    if not user_id:
        # Anonymous click (no user_id) — let it through; authz already
        # blocks unauthenticated paths.
        return True
    now = time.time()
    cutoff = now - _RATE_WINDOW_SEC
    with _rate_lock:
        clicks = _rate_state.setdefault(user_id, deque())
        # Evict expired
        while clicks and clicks[0] < cutoff:
            clicks.popleft()
        if len(clicks) >= cap:
            return False
        clicks.append(now)
        return True


def _deny_apply_if_unauthorized(payload: dict, user_id: str, incident_id: str):
    """Authz + rate-limit gate для apply-кнопок (реальный kubectl write).

    Возвращает _ephemeral-Response при отказе, иначе None. Тот же fail-closed
    whitelist, что и approve/decline — раньше apply-ветки его НЕ вызывали, поэтому
    любой кликнувший в канале мог запустить write мимо DISCORD_APPROVERS_*.
    """
    allowed, reason = _is_authorized_approver(payload)
    if not allowed:
        audit_service.log_event(
            "EXECUTOR_APPLY_DENIED_UNAUTHORIZED",
            {"incident_id": incident_id, "discord_user_id": user_id, "reason": reason},
        )
        return _ephemeral("You are not authorized to apply actions for this incident.")
    if not _check_rate_limit(user_id):
        audit_service.log_event(
            "EXECUTOR_APPLY_DENIED_RATE_LIMIT",
            {"incident_id": incident_id, "discord_user_id": user_id},
        )
        return _ephemeral("Rate limit exceeded — too many apply clicks in the last hour.")
    return None


def _deny_feedback_if_unauthorized(payload: dict, user_id: str, incident_id: str):
    """Authz-gate для feedback-кнопок (👍/👎) — тот же fail-closed whitelist.

    Возвращает _ephemeral-Response при отказе, иначе None. Раньше feedback-
    ветки не проверяли ничего — любой участник гильдии мог портить
    accuracy-статистику (is_accepted) от чужого имени.
    """
    allowed, reason = _is_authorized_approver(payload)
    if not allowed:
        audit_service.log_event(
            "DISCORD_FEEDBACK_DENIED_UNAUTHORIZED",
            {"incident_id": incident_id, "discord_user_id": user_id, "reason": reason},
        )
        return _ephemeral("You are not authorized to leave feedback for this incident.")
    return None


# ─── Background-task registry (GC + observability) ──────────────────────────
# asyncio.create_task без сохранённой ссылки может быть собран GC до
# завершения, а исключение внутри таски терялось молча — оператор видел
# «✅ Executor launched» даже когда apply_intent упал. Держим strong-ref в
# module-level set и логируем/аудируем исключения в done-callback.
_BACKGROUND_TASKS: Set[Any] = set()


def _spawn_background_task(
    coro: Coroutine[Any, Any, Any], *, context: Dict[str, Any]
) -> "asyncio.Task[Any]":
    """create_task + strong-ref + done-callback с логом/аудитом исключений."""
    task: "asyncio.Task[Any]" = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)

    def _on_done(t: Any) -> None:
        _BACKGROUND_TASKS.discard(t)
        try:
            if t.cancelled():
                logger.warning("discord_background_task_cancelled context=%s", context)
                return
            exc = t.exception()
        except Exception:
            return
        if exc is not None:
            logger.error(
                "discord_background_task_failed context=%s error=%s",
                context, repr(exc),
            )
            audit_service.log_event(
                "DISCORD_BACKGROUND_TASK_FAILED",
                {**context, "error": type(exc).__name__},
            )

    task.add_done_callback(_on_done)
    return task


def _verify_signature(public_key_hex: str, signature_hex: str, timestamp: str, body: bytes) -> bool:
    # Anti-replay: timestamp должен быть свежим. Ed25519-подпись валидна вечно,
    # поэтому без окна перехваченный apply/approve-запрос можно переиграть и
    # повторно запустить kubectl. Stale → отказ (как невалидная подпись).
    if not is_timestamp_fresh(timestamp, settings.DISCORD_INTERACTION_MAX_AGE_SECONDS):
        logger.warning("discord_signature_stale_timestamp ts=%s", timestamp)
        return False
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


async def _apply_in_background(
    incident_id: str, user_id: str, intent_sig: str, interaction_token: str
) -> None:
    """Запустить apply_intent + отправить followup-ответ.

    apply_intent — sync (subprocess), запускаем в to_thread; результат форматируем
    тем же _format_apply_result-style как раньше и PATCH в Discord. intent_sig
    прокидывается в apply_intent как expected_signature (TOCTOU-сверка).
    """
    import asyncio
    from app.services.executor_apply import apply_intent
    outcome = await asyncio.to_thread(apply_intent, incident_id, user_id, intent_sig)

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


def _confirm_apply_buttons(incident_id: str, intent_sig: str) -> list:
    """Двухшаговое подтверждение запуска kubectl. Защита от случайного клика.

    intent_sig вшивается в custom_id (colon-формат, как approve/decline), чтобы
    apply_confirm мог прокинуть его в apply_intent для TOCTOU-сверки.
    """
    return [{
        "type": _ACTION_ROW,
        "components": [
            {
                "type": _BUTTON,
                "style": _BTN_DANGER,
                "label": "Да, запустить kubectl",
                "custom_id": f"apply_confirm:{incident_id}:{intent_sig}",
            },
            {
                "type": _BUTTON,
                "style": _BTN_SECONDARY,
                "label": "Отмена",
                "custom_id": f"apply_cancel:{incident_id}",
            },
        ],
    }]


_APPLY_REFUSAL_MESSAGES = {
    "incident_not_found": "❌ Инцидент `{incident_id}` не найден.",
    "no_intent":          "❌ Нет ExecutionIntent для применения — пайплайн не выдал структурный фикс.",
    "already_applied":    "ℹ️ Уже применено ранее — действие идемпотентно.",
    "apply_in_flight":    "ℹ️ Apply уже выполняется (или завершился без записи результата) — повтор заблокирован до истечения claim-TTL.",
    "dry_run_not_ok":     "❌ dry-run не прошёл — реальный запуск заблокирован.",
    "signature_required": "❌ Подпись действия отсутствует — apply не запущен.",
    "signature_mismatch": "❌ Действие изменилось с момента подтверждения (signature mismatch) — повтори триаж.",
    "not_approved":       "❌ Нет записи об одобрении (kg_action_approvals) — apply заблокирован.",
    "approval_stale":     "❌ Одобрение протухло (старше допустимого окна) — нужен свежий approve.",
    "namespace_unbound":  "❌ У инцидента нет namespace — сверка intent ↔ инцидент невозможна, apply заблокирован.",
    "namespace_mismatch": "❌ Namespace действия не совпадает с namespace инцидента — apply заблокирован.",
}


def _format_apply_refusal(incident_id: str, reason: str) -> str:
    """Подобрать человекочитаемое сообщение для отказа apply-flow."""
    # Ключ может быть составной: "risk_too_high:high", "dry_run_not_ok:guardrail_blocked" и т.п.
    key = reason.split(":", 1)[0]
    if key in _APPLY_REFUSAL_MESSAGES:
        return _APPLY_REFUSAL_MESSAGES[key].format(incident_id=incident_id)
    if key == "risk_too_high":
        return "❌ Action risk=`high` — auto-apply запрещён, действуй вручную."
    if key == "policy_block":
        axis = reason.split(":", 1)[1] if ":" in reason else "policy"
        return (
            f"❌ Policy-gate заблокировал действие (`{axis}`): prod/system ns, "
            "data-plane, необратимое или low-confidence. Действуй вручную."
        )
    if key == "intent_invalid":
        return "❌ ExecutionIntent невалиден после round-trip — manual triage."
    return f"❌ Не могу применить: {reason}"


def _record_decision(
    incident_id: str,
    intent_signature: str,
    status: str,
    approved_by: str,
) -> dict:
    """Записать approve/decline в kg_action_approvals.

    Возвращает dict:
      - {"already_decided": False, "status": ..., "approved_by": ..., "decided_at": ...}
        при свежем insert'е;
      - {"already_decided": True, "status": <prev>, "approved_by": <prev>, "decided_at": <prev>}
        если есть существующая запись (UNIQUE collision).
    """
    from app.knowledge_graph.schema import ActionApproval

    db: Session = SessionLocal()
    try:
        decided_at = datetime.now(timezone.utc).replace(tzinfo=None)
        row = ActionApproval(
            incident_id=incident_id,
            intent_signature=intent_signature,
            status=status,
            approved_by=approved_by,
            decided_at=decided_at,
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = (
                db.query(ActionApproval)
                .filter(
                    ActionApproval.incident_id == incident_id,
                    ActionApproval.intent_signature == intent_signature,
                )
                .first()
            )
            if existing is None:
                # Race — кто-то другой удалил после rollback. Не возможно
                # в обычном flow; вернуть already_decided unknown.
                return {
                    "already_decided": True,
                    "status": "unknown",
                    "approved_by": "?",
                    "decided_at": "?",
                }
            return {
                "already_decided": True,
                "status": existing.status,
                "approved_by": existing.approved_by or "?",
                "decided_at": (
                    existing.decided_at.strftime("%H:%M UTC")
                    if existing.decided_at else "?"
                ),
            }
        return {
            "already_decided": False,
            "status": status,
            "approved_by": approved_by,
            "decided_at": decided_at.strftime("%H:%M UTC"),
        }
    finally:
        db.close()


async def _edit_message_after_decision(
    channel_id: str,
    message_id: str,
    verdict: str,
    user_name: str,
    decided_at: str,
    original_message: dict,
) -> None:
    """PATCH сообщения: убрать buttons + дописать пометку в embed footer.

    Используется bot API (PATCH /channels/{cid}/messages/{mid}). Не падаем
    наружу — приоритет на ack пользователю; edit best-effort.
    """
    token = settings.DISCORD_BOT_TOKEN
    if not token:
        logger.warning("discord_edit_skipped reason=no_bot_token")
        return

    # Сохраняем embeds, добавляем пометку в footer.text. Buttons убираем
    # передачей пустого components-массива (Discord допускает []).
    embeds = list(original_message.get("embeds") or [])
    icon = "✅" if verdict == "approved" else "❌"
    mark = f"{icon} {verdict.capitalize()} by @{user_name} at {decided_at}"
    if embeds:
        first = embeds[0]
        existing_footer = (first.get("footer") or {}).get("text") or ""
        new_text = f"{existing_footer} · {mark}" if existing_footer else mark
        first["footer"] = {"text": new_text[:2048]}

    import httpx
    url = f"{_DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}"
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }
    payload = {"embeds": embeds, "components": []}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.patch(url, headers=headers, json=payload)
            if r.status_code >= 400:
                logger.error(
                    "discord_message_edit_failed status=%s body=%s",
                    r.status_code, r.text[:200],
                )
    except Exception as e:
        logger.error("discord_message_edit_exception error=%s", str(e))


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
    # Authz как у apply/approve-путей (fail-closed whitelist): фидбек пишет
    # is_accepted/user_feedback в БД и кормит accuracy-статистику — без гейта
    # любой участник гильдии мог её отравить.
    if custom_id.startswith("feedback_pos_"):
        incident_id = custom_id[len("feedback_pos_"):]
        denied = _deny_feedback_if_unauthorized(payload, user_id, incident_id)
        if denied is not None:
            return denied
        found = _store_feedback(incident_id, "positive", user_id)
        if not found:
            return _ephemeral(f"Инцидент `{incident_id}` не найден в БД.")
        return _ephemeral("✅ Отмечено как **верное решение**. Спасибо!")

    # ── 👎 Шаг 1: запрашиваем подтверждение, ничего не сохраняем ────────────
    if custom_id.startswith("feedback_neg_") and not custom_id.startswith("feedback_neg_confirm_") and not custom_id.startswith("feedback_neg_cancel_"):
        incident_id = custom_id[len("feedback_neg_"):]
        denied = _deny_feedback_if_unauthorized(payload, user_id, incident_id)
        if denied is not None:
            return denied
        return _ephemeral(
            "⚠️ Подтверди: **выводы модели были ошибочными**?\n"
            "-# (не сам алерт, а анализ причины и рекомендации)",
            components=_confirm_neg_buttons(incident_id),
        )

    # ── 👎 Шаг 2: подтверждение — сохраняем негативный фидбек ───────────────
    if custom_id.startswith("feedback_neg_confirm_"):
        incident_id = custom_id[len("feedback_neg_confirm_"):]
        denied = _deny_feedback_if_unauthorized(payload, user_id, incident_id)
        if denied is not None:
            return denied
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
    # custom_id формат `apply:{incident_id}:{intent_signature}` (colon, как
    # approve/decline — incident_id содержит подчёркивания). Подпись несём
    # дальше в apply_confirm для TOCTOU-сверки.
    if custom_id.startswith("apply:"):
        parts = custom_id.split(":", 2)
        if len(parts) != 3:
            return _ephemeral("❌ Неверный формат custom_id.")
        _, incident_id, intent_sig = parts
        if not settings.EXECUTOR_APPROVAL_ENABLED:
            return _ephemeral("❌ EXECUTOR_APPROVAL_ENABLED=false — apply отключён.")
        denied = _deny_apply_if_unauthorized(payload, user_id, incident_id)
        if denied is not None:
            return denied
        return _ephemeral(
            "⚠️ Запустить **kubectl** для этой команды? "
            "dry-run уже прошёл (kube-apiserver валидировал команду), "
            "сейчас будет реальный write.\n"
            "-# Действие записывается в audit + OTEL.",
            components=_confirm_apply_buttons(incident_id, intent_sig),
        )

    # ── ⚙️ Apply: шаг 2, выполнить kubectl (deferred response) ──────────────
    if custom_id.startswith("apply_confirm:"):
        parts = custom_id.split(":", 2)
        if len(parts) != 3:
            return _ephemeral("❌ Неверный формат custom_id.")
        _, incident_id, intent_sig = parts
        if not settings.EXECUTOR_APPROVAL_ENABLED:
            return _ephemeral("❌ EXECUTOR_APPROVAL_ENABLED=false — apply отключён.")
        denied = _deny_apply_if_unauthorized(payload, user_id, incident_id)
        if denied is not None:
            return denied

        interaction_token = payload.get("token", "")
        if not interaction_token:
            # Без token-а не сможем сделать followup; fallback на sync-режим.
            return _ephemeral("❌ Discord interaction token отсутствует — apply не запущен.")

        # Клик «Да, запустить kubectl» = одобрение человеком. Фиксируем его в
        # kg_action_approvals (как approve-кнопка), чтобы apply_intent мог
        # НЕЗАВИСИМО проверить терминальный APPROVED. UNIQUE(incident_id, sig)
        # даёт дедуп; declined-коллизия → apply отменяется.
        member = payload.get("member") or {}
        user_obj = member.get("user") or payload.get("user") or {}
        user_name = user_obj.get("username") or user_obj.get("global_name") or user_id or "unknown"
        decision = _record_decision(incident_id, intent_sig, "approved", user_name)
        if decision["already_decided"] and decision["status"] != "approved":
            return _ephemeral(f"ℹ️ Уже {decision['status']} — apply отменён.")

        # Сразу возвращаем "thinking..." (type=5), apply работает в background-task.
        # Discord даёт 15 минут на followup через PATCH @original — этого хватит
        # даже для большого rollout restart с тяжёлыми initContainers.
        # Strong-ref + done-callback: см. _spawn_background_task.
        _spawn_background_task(
            _apply_in_background(incident_id, user_id, intent_sig, interaction_token),
            context={"kind": "apply_confirm", "incident_id": incident_id,
                     "discord_user_id": user_id},
        )
        return _deferred_ephemeral()

    # ── ⚙️ Apply: отмена ───────────────────────────────────────────────────
    if custom_id.startswith("apply_cancel:"):
        return _ephemeral("Apply отменён.")

    # ── ✅/❌ Approve / Decline proposed action ─────────────────────────────
    # custom_id формат `approve:{incident_id}:{intent_signature}` /
    # `decline:{incident_id}:{intent_signature}`. Двоеточие — потому что
    # incident_id может содержать подчёркивания (см. fingerprint-format).
    if custom_id.startswith("approve:") or custom_id.startswith("decline:"):
        verdict = "approved" if custom_id.startswith("approve:") else "declined"
        try:
            _, incident_id, intent_sig = custom_id.split(":", 2)
        except ValueError:
            return _ephemeral("❌ Неверный формат custom_id.")

        # Кто нажал — для аудита и edit-message
        member = payload.get("member") or {}
        user_obj = member.get("user") or payload.get("user") or {}
        user_name = user_obj.get("username") or user_obj.get("global_name") or user_id or "unknown"

        # ── Authorization gate ───────────────────────────────────────────────
        # Fail-closed: if neither DISCORD_APPROVERS_USER_IDS nor _ROLE_IDS is
        # set, every click is denied. Otherwise the clicker must appear in
        # the user-whitelist OR have at least one role in the role-whitelist.
        # Audit every denial so we can spot abuse / brute-force.
        allowed, reason = _is_authorized_approver(payload)
        if not allowed:
            event_type = (
                "DISCORD_APPROVAL_DENIED_NO_APPROVERS_CONFIGURED"
                if reason == "no_approvers_configured"
                else "DISCORD_APPROVAL_DENIED_UNAUTHORIZED"
            )
            audit_service.log_event(
                event_type,
                {
                    "incident_id": incident_id,
                    "intent_signature": intent_sig,
                    "discord_user_id": user_id,
                    "discord_user_name": user_name,
                    "verdict_attempted": verdict,
                    "reason": reason,
                },
            )
            return _ephemeral(
                "You are not authorized to approve actions for this incident."
            )

        # ── Per-user rate-limit (soft) ──────────────────────────────────────
        # Cap accidental / malicious click-floods. In-memory per process
        # (multiple workers → cap multiplies; acceptable for current scale).
        if not _check_rate_limit(user_id):
            audit_service.log_event(
                "DISCORD_APPROVAL_DENIED_RATE_LIMIT",
                {
                    "incident_id": incident_id,
                    "intent_signature": intent_sig,
                    "discord_user_id": user_id,
                    "discord_user_name": user_name,
                    "verdict_attempted": verdict,
                },
            )
            return _ephemeral(
                "Rate limit exceeded — too many approval clicks in the last hour."
            )

        # Сообщение, на которое прицеплены кнопки — нужно для PATCH'а
        message = payload.get("message") or {}
        message_id = message.get("id")
        channel_id = payload.get("channel_id") or message.get("channel_id")

        decision = _record_decision(
            incident_id=incident_id,
            intent_signature=intent_sig,
            status=verdict,
            approved_by=user_name,
        )

        if decision["already_decided"]:
            prev_status = decision["status"]
            prev_user = decision["approved_by"] or "?"
            prev_time = decision["decided_at"] or "?"
            return _ephemeral(
                f"ℹ️ Already {prev_status} by @{prev_user} at {prev_time}."
            )

        # Edit оригинального сообщения: убрать buttons + дописать footer.
        # Не блокируем основной response — Discord ждёт ≤3s.
        if message_id and channel_id:
            _spawn_background_task(
                _edit_message_after_decision(
                    channel_id=channel_id,
                    message_id=message_id,
                    verdict=verdict,
                    user_name=user_name,
                    decided_at=decision["decided_at"],
                    original_message=message,
                ),
                context={"kind": "edit_after_decision", "incident_id": incident_id},
            )

        audit_service.log_event(
            "INCIDENT_ACTION_APPROVED" if verdict == "approved" else "INCIDENT_ACTION_DECLINED",
            {
                "incident_id": incident_id,
                "intent_signature": intent_sig,
                "discord_user_id": user_id,
                "discord_user_name": user_name,
            },
        )

        if verdict == "declined":
            return _ephemeral(f"❌ Declined. Recorded as decided by @{user_name}.")

        # Approved — dispatch реального write ТОЛЬКО при обоих opt-in флагах.
        # EXECUTOR_ENABLED по README = dry-run-only валидация; prod opt-in на
        # реальный kubectl — EXECUTOR_APPROVAL_ENABLED. Раньше approve-кнопка
        # запускала apply_intent на одном EXECUTOR_ENABLED — реальный write был
        # достижим через флаг, задокументированный как dry-run-only.
        if settings.EXECUTOR_ENABLED and settings.EXECUTOR_APPROVAL_ENABLED:
            try:
                from app.services.executor_apply import apply_intent
                # intent_sig из custom_id → integrity-сверка в apply_intent (TOCTOU).
                # Strong-ref + done-callback: упавший apply_intent логируется и
                # аудируется, а не исчезает молча.
                _spawn_background_task(
                    asyncio.to_thread(apply_intent, incident_id, user_name, intent_sig),
                    context={"kind": "approve_apply", "incident_id": incident_id,
                             "intent_signature": intent_sig,
                             "approved_by": user_name},
                )
                return _ephemeral(
                    f"✅ Approved by @{user_name}. Executor launched в фоне — "
                    "итог (включая отказ) смотри в audit-trail "
                    "(EXECUTOR_APPLIED / EXECUTOR_APPLY_REFUSED)."
                )
            except Exception as e:
                logger.error("approved_executor_dispatch_failed error=%s", str(e))
                return _ephemeral(
                    f"✅ Approved by @{user_name}, но launch executor упал: {e}"
                )
        if settings.EXECUTOR_ENABLED:
            # Fail-closed зеркально apply:/apply_confirm: — решение записано,
            # но реальный write без EXECUTOR_APPROVAL_ENABLED не запускается.
            return _ephemeral(
                f"✅ Approved by @{user_name} (записано), но "
                "EXECUTOR_APPROVAL_ENABLED=false — реальный запуск отключён."
            )
        return _ephemeral(
            f"✅ Approved by @{user_name}. Will execute when executor goes live "
            "(EXECUTOR_ENABLED=false right now)."
        )

    return _ephemeral("Неизвестное действие.")
