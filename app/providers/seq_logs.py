"""`LogProvider` поверх Seq — первая реализация интерфейса.

Тонкий адаптер: вся работа с Seq REST остаётся в
`app/context/seq_client.py`, здесь переводится словарь. Задача адаптера
ровно одна и она не косметическая — перевести способ Seq сообщать об
отказе (`SeqQueryError`) в общий язык измеримости (`Measurement`), чтобы
потребитель не знал ни про Seq, ни про его исключения.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, Optional, Sequence

from app.context.seq_client import SeqClient, SeqQueryError
from app.providers.logs import LogProvider, Measurement, ServiceLogStats

log = logging.getLogger(__name__)


class SeqLogProvider(LogProvider):
    """Логи из Seq-инстанса.

    Конструируется на инстанс: в WO их несколько (prod / preprod /
    per-host), и `name` каждого попадает в `kg_log_observations.source` —
    иначе потом не сказать, чьё именно окно молчало.
    """

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: Optional[str] = None,
        timeout: float = 10.0,
    ) -> None:
        self.name = name
        self._client = SeqClient(base_url=base_url, api_key=api_key, timeout=timeout)

    async def count_events(
        self, level: str, since: datetime, until: datetime
    ) -> Measurement[int]:
        try:
            total, capped = await self._client.count_events_detailed(
                level, since, until
            )
        except SeqQueryError as e:
            # Отказ источника — не ноль. Именно на этом различии стоит весь
            # интерфейс: ноль, означающий «Seq не ответил», однажды уже
            # проходил дальше как «ошибок нет» (20.08.2026, 12,8 часа).
            log.warning(
                "seq_provider.count_unmeasured source=%s level=%s err=%s",
                self.name, level, e,
            )
            return Measurement.unknown(f"seq_unavailable: {e}")
        # capped=True: реальный объём больше. Это по-прежнему измерение,
        # но нижней оценкой — и потребитель должен видеть разницу.
        return Measurement.of(total, exact=not capped)

    async def service_stats(
        self, level: str, since: datetime, until: datetime, limit: int = 500
    ) -> Measurement[Dict[Optional[str], ServiceLogStats]]:
        try:
            events = await self._client.top_messages(
                level=level, since=since, until=until, limit=limit,
            )
        except SeqQueryError as e:
            log.warning(
                "seq_provider.stats_unmeasured source=%s level=%s err=%s",
                self.name, level, e,
            )
            return Measurement.unknown(f"seq_unavailable: {e}")

        grouped = SeqClient.aggregate_by_service(events)
        stats: Dict[Optional[str], ServiceLogStats] = {}
        for app_name, (total, counter) in grouped.items():
            top_message = counter.most_common(1)[0][0] if counter else ""
            stats[app_name] = ServiceLogStats(
                service=app_name,
                count=total,
                top_message=top_message,
                message_counts=dict(counter),
            )
        # Пустой словарь при успешном запросе — измеренная тишина, и это
        # честный ответ: событий за окно не было. Отличается от unknown
        # выше ровно тем, что источник ответил.
        #
        # `exact` завязан на limit: набрав ровно limit событий, мы не знаем,
        # были ли ещё — счёт по сервисам становится нижней оценкой.
        return Measurement.of(stats, exact=len(events) < limit)

    async def sample_messages(
        self, level: str, since: datetime, until: datetime, limit: int = 10
    ) -> Measurement[Sequence[str]]:
        try:
            events = await self._client.top_messages(
                level=level, since=since, until=until, limit=limit,
            )
        except SeqQueryError as e:
            return Measurement.unknown(f"seq_unavailable: {e}")
        messages = [
            msg for msg in (SeqClient.extract_message_template(e) for e in events) if msg
        ]
        return Measurement.of(messages)
