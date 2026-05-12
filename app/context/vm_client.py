"""VictoriaMetrics HTTP client для enrichment диагностического контекста.

Запрашивает метрики пода за N минут до инцидента. Результат идёт в
diag_ctx["metrics_summary"] и используется ResourcePressureRule.

Конфигурация:
    VICTORIA_METRICS_URL — base URL VMSingle/VMSelect.
        In-cluster:   http://vmsingle-vm-victoria-metrics-k8s-stack.monitoring:8428
        Local dev:    http://localhost:8428  (после kubectl port-forward)
        Пусто = метрики отключены (graceful degrade).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# Пороги для структурных флагов.
# memory: >85% лимита в среднем за окно = pressure.
_MEM_PRESSURE_PCT = 0.85
# cpu: throttled_ratio >20% = pressure.
_CPU_THROTTLE_PCT = 0.20


class VMClient:
    """Тонкая обёртка над VictoriaMetrics /api/v1/query_range."""

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self._url = base_url.rstrip("/")
        self._timeout = timeout

    async def query_range(
        self,
        query: str,
        start: datetime,
        end: datetime,
        step: str = "60s",
    ) -> List[Dict[str, Any]]:
        """Выполнить instant range-query. Возвращает список series."""
        params = {
            "query": query,
            "start": start.timestamp(),
            "end": end.timestamp(),
            "step": step,
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.get(f"{self._url}/api/v1/query_range", params=params)
            r.raise_for_status()
            data = r.json()
        if data.get("status") != "success":
            raise RuntimeError(f"VM query failed: {data.get('error', data)}")
        return data.get("data", {}).get("result", [])

    async def get_pod_metrics(
        self,
        namespace: str,
        pod: str,
        window_minutes: int = 15,
        incident_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Собрать memory/CPU метрики пода за window перед инцидентом.

        Возвращает dict совместимый с ResourcePressureRule.metrics_summary:
            memory_pressure  bool   — пиковое потребление > 85% лимита
            cpu_pressure     bool   — CPU throttle ratio > 20%
            memory_trend     str    — "rising" | "stable" | "spike" | "unknown"
            peak_memory_bytes   int
            memory_limit_bytes  int (0 если лимит не задан)
            memory_pct          float  (0.0–1.0, NaN если нет лимита)
            cpu_throttle_ratio  float
        """
        end = incident_time or datetime.now(timezone.utc)
        start = end - timedelta(minutes=window_minutes)

        result: Dict[str, Any] = {
            "memory_pressure": False,
            "cpu_pressure": False,
            "memory_trend": "unknown",
            "peak_memory_bytes": 0,
            "memory_limit_bytes": 0,
            "memory_pct": 0.0,
            "cpu_throttle_ratio": 0.0,
        }

        try:
            mem_series, limit_series, throttle_series = await asyncio.gather(
                self.query_range(
                    f'container_memory_working_set_bytes{{namespace="{namespace}",pod=~"{pod}.*",container!=""}}',
                    start, end,
                ),
                self.query_range(
                    f'kube_pod_container_resource_limits{{namespace="{namespace}",pod=~"{pod}.*",resource="memory"}}',
                    start, end,
                ),
                self.query_range(
                    f'rate(container_cpu_cfs_throttled_seconds_total{{namespace="{namespace}",pod=~"{pod}.*",container!=""}}[5m])'
                    f' / rate(container_cpu_cfs_periods_total{{namespace="{namespace}",pod=~"{pod}.*",container!=""}}[5m])',
                    start, end,
                ),
                return_exceptions=True,
            )

            # Memory working set
            if isinstance(mem_series, list) and mem_series:
                all_vals = [
                    float(v[1]) for s in mem_series for v in s.get("values", [])
                    if v[1] not in ("NaN", "Inf", "+Inf")
                ]
                if all_vals:
                    peak = max(all_vals)
                    result["peak_memory_bytes"] = int(peak)
                    result["memory_trend"] = _classify_trend(all_vals)

            # Memory limit
            if isinstance(limit_series, list) and limit_series:
                limit_vals = [
                    float(v[1]) for s in limit_series for v in s.get("values", [])
                    if v[1] not in ("NaN", "Inf", "+Inf")
                ]
                if limit_vals:
                    limit = limit_vals[-1]
                    result["memory_limit_bytes"] = int(limit)
                    if limit > 0 and result["peak_memory_bytes"]:
                        pct = result["peak_memory_bytes"] / limit
                        result["memory_pct"] = round(pct, 3)
                        result["memory_pressure"] = pct > _MEM_PRESSURE_PCT

            # CPU throttle
            if isinstance(throttle_series, list) and throttle_series:
                thr_vals = [
                    float(v[1]) for s in throttle_series for v in s.get("values", [])
                    if v[1] not in ("NaN", "Inf", "+Inf")
                ]
                if thr_vals:
                    ratio = sum(thr_vals) / len(thr_vals)
                    result["cpu_throttle_ratio"] = round(ratio, 3)
                    result["cpu_pressure"] = ratio > _CPU_THROTTLE_PCT

        except Exception as e:
            logger.warning("vm_client.get_pod_metrics failed: %s", e)

        return result


def _classify_trend(values: List[float]) -> str:
    """Classify memory trend: rising / stable / spike."""
    if len(values) < 4:
        return "unknown"
    mid = len(values) // 2
    first_half_avg = sum(values[:mid]) / mid
    second_half_avg = sum(values[mid:]) / (len(values) - mid)
    peak = max(values)
    avg = sum(values) / len(values)

    if second_half_avg > first_half_avg * 1.25:
        return "rising"
    if peak > avg * 1.8:
        return "spike"
    return "stable"
