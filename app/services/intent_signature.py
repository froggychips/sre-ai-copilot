"""Идентичность и происхождение ExecutionIntent: signature + время фиксации.

Два вопроса про «что именно подтвердил оператор» живут здесь вместе:
  - ЧТО он видел — compute_signature (детерминированный хэш intent-а);
  - КОГДА это было посчитано — intent_recorded_at (якорь свежести плана).

Signature используется в Discord approve/decline buttons как custom_id-компонент:
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
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

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


def parse_utc_ts(raw: Any) -> Optional[datetime]:
    """ISO-строка/datetime → aware UTC datetime. Мусор → None (caller fail-closed)."""
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    # Пайплайн пишет aware-ISO, а DateTime-колонки БД возвращают naive UTC.
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def intent_recorded_at(
    analysis: Dict[str, Any], fallback: Any = None
) -> Tuple[Optional[datetime], str]:
    """Когда был посчитан intent/dry-run, которые видел оператор.

    Нужно apply-флоу: approval-окно ограничивает время от клика Approve, но не
    возраст самого плана. kubectl, посчитанный по состоянию кластера недельной
    давности, применять к текущему кластеру нельзя (см. executor_apply).

    Возвращает `(aware UTC datetime | None, источник)`. Порядок — от точного
    якоря к грубому; берётся ПЕРВЫЙ доступный, а не минимум: грубый fallback
    относится к другому событию (созданию инцидента), и смешивать их нельзя.

      1. `executor_result.dry_run_at` / `.completed_at` и
         `execution_intent_at` в analysis — явные stamp-поля стадии executor.
         На сегодня пайплайн их НЕ пишет (app/workers/pipeline.py вне скоупа
         этой правки), читаем forward-compatible: как только появятся — станут
         основным якорём без изменений здесь.
      2. `report_sent.sent_at` — момент, когда embed с кнопкой Apply реально
         ушёл в Discord. Это ровно тот прогон, в котором посчитаны
         execution_intent/executor_result → ближайший доступный к ним якорь.
      3. `report_pending.queued_at` — коммит терминального состояния прогона
         (отчёт ещё не доставлен). Пишется ТОЙ ЖЕ транзакцией, что и
         execution_intent/executor_result в _persist.
      4. `fallback` — обычно `IncidentRecord.created_at`, время создания строки
         инцидента. Грубее (первый fire мог быть раньше прогона), но всегда НЕ
         ПОЗЖЕ intent-а, поэтому возраст получается завышенным — проверка
         строже, а не мягче.

    Ни одного якоря → `(None, "unknown")`: caller обязан отказать (fail-closed),
    возраст плана неизвестен.
    """
    analysis = analysis if isinstance(analysis, dict) else {}
    executor_result = analysis.get("executor_result")
    executor_result = executor_result if isinstance(executor_result, dict) else {}
    report_sent = analysis.get("report_sent")
    report_sent = report_sent if isinstance(report_sent, dict) else {}
    report_pending = analysis.get("report_pending")
    report_pending = report_pending if isinstance(report_pending, dict) else {}

    candidates = (
        ("executor_result.dry_run_at", executor_result.get("dry_run_at")),
        ("executor_result.completed_at", executor_result.get("completed_at")),
        ("analysis.execution_intent_at", analysis.get("execution_intent_at")),
        ("report_sent.sent_at", report_sent.get("sent_at")),
        ("report_pending.queued_at", report_pending.get("queued_at")),
        ("incident.created_at", fallback),
    )
    for source, raw in candidates:
        parsed = parse_utc_ts(raw)
        if parsed is not None:
            return parsed, source
    return None, "unknown"
