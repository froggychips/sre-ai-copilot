"""`MetricsProvider` поверх VictoriaMetrics.

Тонкий адаптер: работа с HTTP-API остаётся в `app/context/vm_client.py`,
здесь переводится словарь. Задача одна и не косметическая — превратить
способ VictoriaMetrics сообщать об отказе в общий язык измеримости, чтобы
вызывающий не знал ни про VM, ни про её исключения и не мог случайно
принять молчание источника за отсутствие нагрузки.
"""
from __future__ import annotations

import logging
from typing import Dict, Mapping, Optional, Sequence, Tuple

from app.context.vm_client import VMClient, VMQueryError
from app.providers.measurement import Measurement
from app.providers.metrics import MetricsProvider

log = logging.getLogger(__name__)


class VictoriaMetricsProvider(MetricsProvider):
    """Метрики из VictoriaMetrics."""

    def __init__(
        self, name: str, base_url: str, timeout: float = 10.0
    ) -> None:
        self.name = name
        self._client = VMClient(base_url=base_url, timeout=timeout)

    async def scalar(self, query: str) -> Measurement[float]:
        """Одно число.

        `query_instant` не различает отказ и пустой ответ — оба дают None, —
        и переделывать его контракт ради этого не стоит: у него есть
        потребители, которым None и нужен как «нет данных». Поэтому None
        трактуется здесь консервативно, как незнание: ошибиться в сторону
        «мы не уверены» дешевле, чем выдать тишину за измерение.
        """
        value = await self._client.query_instant(query)
        if value is None:
            return Measurement.unknown("vm_no_value")
        return Measurement.of(value)

    async def by_label(
        self, query: str, label: str
    ) -> Measurement[Dict[str, float]]:
        try:
            series = await self._client.query_instant_by_strict(query, label)
        except VMQueryError as e:
            log.warning(
                "vm_provider.unmeasured source=%s label=%s err=%s",
                self.name, label, e,
            )
            return Measurement.unknown(f"vm_unavailable: {e}")
        # Пустой словарь при успешном запросе — измеренная тишина: серий за
        # окно нет. От unknown выше отличается тем, что источник ответил.
        return Measurement.of(series)

    async def by_labels(
        self, query: str, labels: Sequence[str]
    ) -> Measurement[Mapping[Tuple[str, ...], float]]:
        try:
            series = await self._client.query_instant_by_labels_strict(
                query, tuple(labels)
            )
        except VMQueryError as e:
            log.warning(
                "vm_provider.unmeasured source=%s labels=%s err=%s",
                self.name, ",".join(labels), e,
            )
            return Measurement.unknown(f"vm_unavailable: {e}")
        return Measurement.of(series)


def make_metrics_provider(
    name: str = "vm",
    url: Optional[str] = None,
    timeout: float = 10.0,
    backend: Optional[str] = None,
) -> MetricsProvider:
    """Собрать провайдер метрик.

    `url` по умолчанию берётся из `settings.VICTORIA_METRICS_URL`. Как и у
    логов, неизвестный backend — ошибка конфигурации, а не повод молча
    подставить работающий: опечатка в настройке не должна выглядеть как
    исправная система.
    """
    from app.config import settings

    chosen = (
        backend or getattr(settings, "METRICS_PROVIDER_BACKEND", "vm") or "vm"
    ).lower()
    if chosen != "vm":
        raise ValueError(
            f"неизвестный METRICS_PROVIDER_BACKEND={chosen!r}; поддерживается: vm"
        )
    return VictoriaMetricsProvider(
        name=name,
        base_url=url or settings.VICTORIA_METRICS_URL,
        timeout=timeout,
    )
