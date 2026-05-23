"""Детерминированный signature для ExecutionIntent.

Используется в Discord approve/decline buttons как custom_id-компонент:
`approve:{incident_id}:{intent_signature}` / `decline:...`.

Свойства:
  - Один и тот же intent (action+resource+ns+params) → один и тот же signature
    при любом повторном вычислении (idempotent).
  - Изменение risk не меняет signature — пользователь подтверждает СУТЬ
    действия, не пометку рисков.
  - 12 hex-символов (48 бит): достаточно для collision-free в рамках
    одного incident'а (один embed = max 1-2 buttons), коротко для Discord
    custom_id-limit (100 символов).
"""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.execution_dsl import ExecutionIntent


def compute_signature(intent: "ExecutionIntent") -> str:
    """Возвращает 12-символьный hex hash от стабильной репрезентации intent."""
    payload = {
        "action": intent.action.value,
        "resource_type": intent.resource_type,
        "resource_name": intent.resource_name,
        "namespace": intent.namespace,
        # params сериализуем sort_keys=True для стабильности (dict-порядок
        # в Python 3.7+ insertion-preserving, но LLM может вернуть в разном порядке).
        "params": intent.params or {},
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]
