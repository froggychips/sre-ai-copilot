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
from typing import Any, Dict, List, Optional, Union

import httpx

from app.services.resilience import with_external_retry

logger = logging.getLogger(__name__)

# Пороги для структурных флагов.
# memory: >85% лимита в среднем за окно = pressure.
_MEM_PRESSURE_PCT = 0.85
# cpu: throttled_ratio >20% = pressure.
_CPU_THROTTLE_PCT = 0.20


class ClusterHealth:
    """Snapshot кластерного здоровья — результат get_cluster_health()."""

    def __init__(self, metrics: Dict[str, Any]) -> None:
        self._m = metrics

    # ── Сырые числа ────────────────────────────────────────────────────────

    @property
    def nodes_ready(self) -> int: return self._m.get("nodes_ready", 0)
    @property
    def nodes_total(self) -> int: return self._m.get("nodes_total", 0)
    @property
    def pods_running(self) -> int: return self._m.get("pods_running", 0)
    @property
    def pods_pending(self) -> int: return self._m.get("pods_pending", 0)
    @property
    def pods_failed(self) -> int: return self._m.get("pods_failed", 0)
    @property
    def crashloops(self) -> int: return self._m.get("crashloops", 0)
    @property
    def deploy_mismatch(self) -> int: return self._m.get("deploy_mismatch", 0)
    @property
    def cpu_pct(self) -> float: return self._m.get("cpu_pct", 0.0)
    @property
    def mem_pct(self) -> float: return self._m.get("mem_pct", 0.0)
    @property
    def disk_peak_pct(self) -> float: return self._m.get("disk_peak_pct", 0.0)
    @property
    def alerts_critical(self) -> int: return self._m.get("alerts_critical", 0)
    @property
    def alerts_warning(self) -> int: return self._m.get("alerts_warning", 0)
    @property
    def alerts_prod(self) -> int: return self._m.get("alerts_prod", 0)

    # ── Производные ────────────────────────────────────────────────────────

    @property
    def data_available(self) -> bool:
        """False когда VictoriaMetrics недоступна и все метрики вернули 0."""
        return self.nodes_total > 0

    @property
    def nodes_ok(self) -> bool:
        return self.nodes_ready == self.nodes_total and self.nodes_total > 0

    @property
    def health_status(self) -> str:
        if not self.data_available:
            return "unknown"
        if self.alerts_prod > 0 or not self.nodes_ok or self.disk_peak_pct > 95:
            return "critical"
        if (self.alerts_critical > 0 or self.crashloops > 0 or
                self.pods_failed > 0 or self.disk_peak_pct > 85 or
                self.deploy_mismatch > 0):
            return "degraded"
        return "healthy"

    def to_prompt_context(self) -> str:
        """Описание для LLM-контекста инцидента."""
        if not self.data_available:
            return "=== CLUSTER HEALTH AT INCIDENT TIME ===\nStatus: UNKNOWN (VictoriaMetrics unavailable)\n"
        return (
            f"=== CLUSTER HEALTH AT INCIDENT TIME ===\n"
            f"Status: {self.health_status.upper()}\n"
            f"Nodes: {self.nodes_ready}/{self.nodes_total} ready\n"
            f"Pods: {self.pods_running} run / {self.pods_pending} pend / {self.pods_failed} fail"
            f" · Crashloops: {self.crashloops} · Deploy mismatch: {self.deploy_mismatch}\n"
            f"Resources: CPU {self.cpu_pct:.1f}% · Mem {self.mem_pct:.1f}% · Disk peak {self.disk_peak_pct:.1f}%\n"
            f"Firing alerts: {self.alerts_critical} critical / {self.alerts_warning} warning"
            f" · PROD: {self.alerts_prod} critical\n"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {**self._m, "health_status": self.health_status}


class VMClient:
    """Тонкая обёртка над VictoriaMetrics /api/v1/query и /api/v1/query_range."""

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self._url = base_url.rstrip("/")
        self._timeout = timeout

    async def query_instant(self, query: str) -> float:
        """Instant query → скалярное значение (0.0 при ошибке или пустом ответе)."""
        params = {"query": query}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.get(f"{self._url}/api/v1/query", params=params)
                r.raise_for_status()
                data = r.json()
            result = data.get("data", {}).get("result", [])
            if result:
                val = result[0].get("value", [None, "0"])[1]
                if val not in ("NaN", "Inf", "+Inf", "-Inf", None):
                    return float(val)
        except Exception as e:
            logger.debug("vm_client.query_instant failed query=%r: %s", query, e)
        return 0.0

    async def query_instant_by(self, query: str, by_label: str) -> Dict[str, float]:
        """Instant query → {label_value: float} по одной метке (напр. "pod").

        Для namespace-агрегированных запросов вида `... by (pod)`: один HTTP
        вместо N per-service. Серии без нужной метки или с NaN/Inf
        отбрасываются. Пустой dict при ошибке/пустом ответе (graceful degrade).
        """
        out: Dict[str, float] = {}
        params = {"query": query}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.get(f"{self._url}/api/v1/query", params=params)
                r.raise_for_status()
                data = r.json()
            for series in data.get("data", {}).get("result", []):
                key = series.get("metric", {}).get(by_label)
                if not key:
                    continue
                val = series.get("value", [None, None])[1]
                if val in ("NaN", "Inf", "+Inf", "-Inf", None):
                    continue
                try:
                    out[str(key)] = float(val)
                except (TypeError, ValueError):
                    continue
        except Exception as e:
            logger.debug("vm_client.query_instant_by failed query=%r: %s", query, e)
        return out

    @with_external_retry(max_attempts=3, initial_delay=0.5, name="vm.cluster_health")
    async def get_cluster_health(self) -> ClusterHealth:
        """Cluster-wide health snapshot — те же метрики что в #stats daily report.

        Запросы идентичны report.sh из configmap/cluster-health-report-scripts.
        Все 13 запросов параллельны; отдельный таймаут 10 сек на каждый.
        """
        _ACT = 'alertstate="firing",severity=~"warning|critical",alertname!~"Watchdog|InfoInhibitor"'
        _ACT_CRIT = _ACT.replace("warning|critical", "critical")
        _ACT_WARN = _ACT.replace("warning|critical", "warning")

        queries = {
            "nodes_total":    'count(kube_node_info)',
            "nodes_ready":    'count(kube_node_status_condition{condition="Ready",status="true"})',
            "pods_running":   'sum(kube_pod_status_phase{phase="Running"})',
            "pods_pending":   'sum(kube_pod_status_phase{phase="Pending"})',
            "pods_failed":    'sum(kube_pod_status_phase{phase="Failed"})',
            "crashloops":     'sum(kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"})',
            "deploy_mismatch":'count(kube_deployment_status_replicas_available != kube_deployment_spec_replicas)',
            "cpu_pct":        'avg(100 - rate(node_cpu_seconds_total{mode="idle"}[5m]) * 100)',
            "mem_pct":        '100 * (1 - sum(node_memory_MemAvailable_bytes) / sum(node_memory_MemTotal_bytes))',
            "disk_peak_pct":  'max(100 * (1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}))',
            "alerts_critical": f'count(ALERTS{{{_ACT_CRIT}}})',
            "alerts_warning":  f'count(ALERTS{{{_ACT_WARN}}})',
            "alerts_prod":    (
                f'count(ALERTS{{{_ACT},instance=~"prod-.+"}} or '
                f'ALERTS{{{_ACT},namespace=~"prod-.+"}})'
            ),
        }

        keys = list(queries.keys())
        values = await asyncio.gather(
            *[self.query_instant(q) for q in queries.values()],
            return_exceptions=True,
        )

        _float_keys = {"cpu_pct", "mem_pct", "disk_peak_pct"}
        metrics: Dict[str, Any] = {}
        for key, val in zip(keys, values):
            safe = 0.0 if isinstance(val, (Exception, BaseException)) else float(val)
            metrics[key] = round(safe, 1) if key in _float_keys else int(safe)

        return ClusterHealth(metrics)

    @with_external_retry(max_attempts=3, initial_delay=0.5, name="vm.query_range")
    async def query_range(
        self,
        query: str,
        start: datetime,
        end: datetime,
        step: str = "60s",
    ) -> List[Dict[str, Any]]:
        """Выполнить instant range-query. Возвращает список series."""
        # Явная аннотация — httpx ждёт Mapping[str, str | int | float | bool | None | Sequence[...]],
        # из dict-literal mypy выводит dict[str, object] и шумит.
        params: Dict[str, Union[str, int, float]] = {
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
            # gather(return_exceptions=True) даёт tuple[list | BaseException, ...] —
            # mypy теряет тип при unpacking, аннотируем явно.
            gathered: tuple[Any, Any, Any] = await asyncio.gather(
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
            mem_series, limit_series, throttle_series = gathered

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
