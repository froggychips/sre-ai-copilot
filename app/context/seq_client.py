"""Seq HTTP client для error/fatal log enrichment.

Seq — log aggregator. Layout в WO:
  * на каждом wo-api{N}-prod (`/seq/`)
  * один в ns `logging`

REST API:
  * `/seq/api/events` — фильтр по filter expression / signal-IDs.
  * signal-m33301 = Errors, signal-m33302 = Warnings (общие WO signal-IDs;
    TODO: уточнить под каждый Seq-instance, могут отличаться).

Аутентификация: anonymous read access работает на dev/preprod, на prod —
требуется API key (X-Seq-ApiKey header).

Используется beat-task'ом `kg_seq_logs_sync_task` (см.
`app/knowledge_graph/seq_logs_sync.py`) для агрегации Error/Fatal событий
per service per window и записи в `kg_log_observations`.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.services.resilience import with_external_retry

logger = logging.getLogger(__name__)


# Сопоставление human-level → Seq @Level. Seq хранит уровни как строки.
_SEQ_LEVELS = {
    "Error": "Error",
    "Fatal": "Fatal",
    "Warning": "Warning",
}


def _iso(ts: datetime) -> str:
    """ISO 8601 без timezone — Seq принимает naive UTC."""
    if ts.tzinfo is not None:
        ts = ts.astimezone(tz=None).replace(tzinfo=None)
    return ts.isoformat(timespec="seconds")


class SeqClient:
    """Тонкая обёртка над Seq `/api/events` для count + top-messages.

    Конструируется per Seq-instance (например, prod / preprod / per wo-api-host).
    Все методы — read-only.
    """

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        timeout: float = 10.0,
    ) -> None:
        # base_url может быть как `https://host/seq` так и `https://host/seq/`
        self._url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    def _headers(self) -> Dict[str, str]:
        h = {"Accept": "application/json"}
        if self._api_key:
            h["X-Seq-ApiKey"] = self._api_key
        return h

    @with_external_retry(max_attempts=2, initial_delay=0.5, name="seq.count")
    async def count_events(
        self,
        level: str,
        since: datetime,
        until: datetime,
    ) -> int:
        """Count событий заданного level за окно [since, until].

        Возвращает 0 при любой ошибке (graceful degrade).
        """
        seq_level = _SEQ_LEVELS.get(level, level)
        # Seq фильтр: `@Level = 'Error'`. fromDateUtc / toDateUtc — naive UTC.
        params: Dict[str, Any] = {
            "filter": f"@Level = '{seq_level}'",
            "fromDateUtc": _iso(since),
            "toDateUtc": _iso(until),
            "count": 1,  # для count мы не тащим payload, только заголовок
            # `shaped=false` — Seq возвращает raw events; нам нужен `Total`.
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.get(
                    f"{self._url}/api/events",
                    params=params,
                    headers=self._headers(),
                )
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            logger.debug(
                "seq_client.count_failed url=%s level=%s err=%s",
                self._url, level, e,
            )
            return 0
        # Seq может вернуть либо {"Total": N, ...} либо list[Event]. Если
        # totals нет — fallback на len(list). Граничный случай: API вернул
        # «approximate total», тогда `Total` всё равно числовой.
        if isinstance(data, dict) and "Total" in data:
            try:
                return int(data["Total"])
            except (TypeError, ValueError):
                return 0
        if isinstance(data, list):
            return len(data)
        return 0

    @with_external_retry(max_attempts=2, initial_delay=0.5, name="seq.events")
    async def top_messages(
        self,
        level: str,
        since: datetime,
        until: datetime,
        limit: int = 200,
        group_field: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Топ событий level за окно. Возвращает список raw Seq events.

        Использует Seq events endpoint с count=limit; группировку
        по Application/service сами делаем в `seq_logs_sync.py` (см.
        `_group_events`). `group_field` — placeholder под будущий
        переход на Seq `/api/signals/aggregate` (TODO: уточнить
        правильное имя поля при первом проде, в WO видел `Application`).
        """
        seq_level = _SEQ_LEVELS.get(level, level)
        params: Dict[str, Any] = {
            "filter": f"@Level = '{seq_level}'",
            "fromDateUtc": _iso(since),
            "toDateUtc": _iso(until),
            "count": limit,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.get(
                    f"{self._url}/api/events",
                    params=params,
                    headers=self._headers(),
                )
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            logger.debug(
                "seq_client.top_failed url=%s level=%s err=%s",
                self._url, level, e,
            )
            return []
        # Seq REST возвращает list[Event] или {"Events": [...]} в зависимости
        # от endpoint-режима. Нормализуем.
        if isinstance(data, dict) and "Events" in data:
            events = data.get("Events") or []
        elif isinstance(data, list):
            events = data
        else:
            events = []
        return [e for e in events if isinstance(e, dict)]

    @staticmethod
    def extract_application(event: Dict[str, Any]) -> Optional[str]:
        """Best-effort выдрать тэг сервиса из Seq event.

        В WO Seq использует поле `Application` (структурированное property)
        или `SourceContext` (NLog-стиль). TODO: финализировать при первом
        запуске на проде — возможно потребуется fallback chain.
        """
        # Properties у Seq лежат либо в Properties[] (legacy) либо
        # в плоских polymorphic-ключах. Покрываем оба формата.
        props = event.get("Properties") or []
        if isinstance(props, list):
            for p in props:
                if not isinstance(p, dict):
                    continue
                name = p.get("Name")
                if name in ("Application", "service", "ServiceName"):
                    val = p.get("Value")
                    if val:
                        return str(val)
        # Плоский формат — например `{"Application": "town-service", ...}`.
        for key in ("Application", "service", "ServiceName"):
            v = event.get(key)
            if v:
                return str(v)
        return None

    @staticmethod
    def extract_message_template(event: Dict[str, Any]) -> str:
        """MessageTemplate стабильнее RenderedMessage (без интерполяции).

        Fallback chain: MessageTemplate → RenderedMessage → Message → "".
        """
        for k in ("MessageTemplate", "RenderedMessage", "Message"):
            v = event.get(k)
            if v:
                return str(v)
        return ""

    @staticmethod
    def aggregate_by_service(
        events: List[Dict[str, Any]],
    ) -> Dict[Optional[str], Tuple[int, Counter]]:
        """Группирует список событий по Application → (count, Counter[msg]).

        Возвращает dict вида `{app_name|None: (total, Counter(msg → freq))}`.
        Используется `seq_logs_sync.py` для подсчёта top-message и hash'а.
        """
        by_app: Dict[Optional[str], Tuple[int, Counter]] = {}
        for ev in events:
            app = SeqClient.extract_application(ev)
            msg = SeqClient.extract_message_template(ev)
            total, counter = by_app.get(app, (0, Counter()))
            total += 1
            if msg:
                counter[msg] += 1
            by_app[app] = (total, counter)
        return by_app
