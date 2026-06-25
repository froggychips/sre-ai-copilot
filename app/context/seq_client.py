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

    # Лимиты пагинации для count_events. Seq `/api/events` НЕ отдаёт `Total` —
    # реальный total считаем перелистыванием страниц по курсору `afterId` и
    # суммированием. Cap нужен чтобы окно с экстремальным error-storm не
    # утащило нас в десятки запросов; при упоре в cap логируем WARNING и
    # возвращаем floor-оценку (НЕ обрезаем молча).
    _COUNT_PAGE_SIZE = 1000
    _COUNT_MAX_PAGES = 20  # ⇒ потолок ~20k событий на окно/level

    @staticmethod
    def _event_id(event: Dict[str, Any]) -> Optional[str]:
        """ID события для курсора пагинации (`afterId`)."""
        for k in ("Id", "id", "EventId"):
            v = event.get(k)
            if v:
                return str(v)
        return None

    @with_external_retry(max_attempts=2, initial_delay=0.5, name="seq.count")
    async def count_events(
        self,
        level: str,
        since: datetime,
        until: datetime,
    ) -> int:
        """Count событий заданного level за окно [since, until].

        Seq REST `/api/events` возвращает СТРАНИЦУ raw-событий (list), без
        конверта `{Total}`. Поэтому реальный total получаем пагинацией:
        тащим страницы по `_COUNT_PAGE_SIZE`, перелистывая по курсору
        `afterId` = Id последнего события предыдущей страницы, и суммируем
        длины, пока страница не короче запрошенной (= хвост) либо не упёрлись
        в `_COUNT_MAX_PAGES`. Если cap достигнут — WARNING и возврат floor-оценки
        (что насчитали), а НЕ тихая обрезка до 1.

        Возвращает 0 при любой ошибке (graceful degrade).
        """
        seq_level = _SEQ_LEVELS.get(level, level)
        # Seq фильтр: `@Level = 'Error'`. fromDateUtc / toDateUtc — naive UTC.
        base_params: Dict[str, Any] = {
            "filter": f"@Level = '{seq_level}'",
            "fromDateUtc": _iso(since),
            "toDateUtc": _iso(until),
            "count": self._COUNT_PAGE_SIZE,
        }
        total = 0
        after_id: Optional[str] = None
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                for page in range(self._COUNT_MAX_PAGES):
                    params = dict(base_params)
                    if after_id is not None:
                        params["afterId"] = after_id
                    r = await client.get(
                        f"{self._url}/api/events",
                        params=params,
                        headers=self._headers(),
                    )
                    r.raise_for_status()
                    data = r.json()
                    # Нормализуем: Seq отдаёт list[Event] либо {"Events": [...]}.
                    if isinstance(data, dict):
                        events = data.get("Events") or []
                    elif isinstance(data, list):
                        events = data
                    else:
                        events = []
                    events = [e for e in events if isinstance(e, dict)]
                    total += len(events)
                    # Хвост: страница короче запрошенной → событий больше нет.
                    if len(events) < self._COUNT_PAGE_SIZE:
                        break
                    # Курсор на следующую страницу. Нет Id → дальше идти не можем.
                    after_id = self._event_id(events[-1])
                    if after_id is None:
                        logger.warning(
                            "seq_client.count_no_cursor url=%s level=%s "
                            "page=%d total=%d — событие без Id, пагинация оборвана",
                            self._url, level, page, total,
                        )
                        break
                else:
                    # for завершился без break ⇒ упёрлись в cap.
                    logger.warning(
                        "seq_client.count_cap_hit url=%s level=%s max_pages=%d "
                        "floor_total=%d — реальный объём БОЛЬШЕ, возвращаю оценку",
                        self._url, level, self._COUNT_MAX_PAGES, total,
                    )
        except Exception as e:
            logger.debug(
                "seq_client.count_failed url=%s level=%s err=%s",
                self._url, level, e,
            )
            return 0
        return total

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

    # Recon 2026-06-05 (per-realm Seq live): реальные WO-события НЕ имеют
    # `Application`; сервис-тег лежит в `App` (.NET assembly, напр.
    # "GR.WO.Bot", "GR.WO.Push.Service"). `ContainerName` обычно пустой,
    # `SourceContext` — FQ-класс. Порядок предпочтения ниже отражает это.
    _APP_PROP_KEYS = ("App", "Application", "ContainerName", "service", "ServiceName")

    @staticmethod
    def extract_application(event: Dict[str, Any]) -> Optional[str]:
        """Best-effort выдрать тэг сервиса (.NET App name) из Seq event.

        Возвращает «сырое» значение `App` (напр. "GR.WO.Bot") — маппинг в
        k8s-имя сервиса делает `seq_logs_sync._match_service`.
        """
        # Properties у Seq лежат либо в Properties[] (legacy) либо
        # в плоских polymorphic-ключах. Покрываем оба формата.
        props = event.get("Properties") or []
        if isinstance(props, list):
            for p in props:
                if not isinstance(p, dict):
                    continue
                if p.get("Name") in SeqClient._APP_PROP_KEYS:
                    val = p.get("Value")
                    if val:
                        return str(val)
        # Плоский формат — например `{"App": "GR.WO.Bot", ...}`.
        for key in SeqClient._APP_PROP_KEYS:
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
