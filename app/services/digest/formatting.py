"""Чистые форматтеры дайджеста: Δ, тренды, возраст, маркеры.

Ни одна функция здесь не ходит в БД, Redis или VM — это делает их
единственным слоем дайджеста, который тестируется таблицей входов и выходов.
В stats_digest они тонули между SQL-секциями, хотя меняются по совершенно
другим поводам (текст для человека, а не источник данных).

Ключевое правило слоя: **None ≠ ноль**. Метрика, которую не получили, обязана
выглядеть как «?», а не как «0» — иначе дайджест сообщает об отсутствии
проблемы там, где на самом деле отсутствуют данные.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional

__all__ = [
    "SNAPSHOT_METRIC_SOURCES",
    "fmt_firing_series_trend",
    "fmt_delta_pp",
    "fmt_delta_int",
    "fmt_snapshot_metric",
    "health_marker",
    "humanize_ago",
    "format_services_list",
    "format_gap_minutes",
]


def fmt_firing_series_trend(today: int, yesterday: Optional[int]) -> str:
    """Trend-суффикс для `Firing series: 673 (+47 vs вчера, +7.5%)`.

    `yesterday is None` — первый запуск, метим `(new baseline)`, а не рисуем
    дельту от нуля. Разница 0 — `(=0 vs вчера)`.
    """
    if yesterday is None:
        return " (new baseline)"
    delta = today - yesterday
    if delta == 0:
        return " (=0 vs вчера)"
    sign = "+" if delta > 0 else ""
    if yesterday > 0:
        pct = (delta / yesterday) * 100
        return f" ({sign}{delta} vs вчера, {sign}{pct:.1f}%)"
    # yesterday=0, today>0 → не делим на ноль, просто +N
    return f" ({sign}{delta} vs вчера)"


def fmt_delta_pp(today: Optional[float], yesterday: Optional[float]) -> str:
    """Δ в percentage-points: '+3pp' / '-1pp' / '±0pp'. None → пусто."""
    if today is None or yesterday is None:
        return ""
    delta = today - yesterday
    if abs(delta) < 0.5:
        return "±0pp"
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta:.0f}pp"


def fmt_delta_int(today: Optional[float], yesterday: Optional[float]) -> str:
    """Δ для integer-метрик (crashloops): '+2' / '-1' / '±0'."""
    if today is None or yesterday is None:
        return ""
    delta = today - yesterday
    if abs(delta) < 0.5:
        return "±0"
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta:.0f}"


# Откуда физически берётся каждая метрика снапшота — чтобы в предупреждении о
# неполном снапшоте было написано, ЧТО чинить, а не абстрактное «нет данных».
# Обе метрики в get_cluster_health() запрашиваются из kube_*, т.е. из KSM:
#   nodes_ready = count(kube_node_status_condition{condition="Ready",...})
#   crashloops  = sum(kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"})
SNAPSHOT_METRIC_SOURCES = {
    "nodes_ready": "kube-state-metrics",
    "crashloops": "kube-state-metrics",
}


def fmt_snapshot_metric(value: Any) -> str:
    """Значение метрики снапшота, где None = «нет данных», а НЕ ноль.

    `VMClient.query_instant` намеренно различает 0.0 и None (см. его контракт),
    а `get_cluster_health` кладёт None в metrics и выставляет
    data_available=False / health_status="unknown". Раньше секция брала
    `d.get("crashloops", "?")` — но дефолт `"?"` не срабатывает, когда ключ
    ЕСТЬ со значением None, и в дайджест уходило литеральное
    `Crashloops: None`. Читается как «крашлупов нет», означает ровно
    обратное — метрику не получили.

    Прецедент 06.08.2026: KSM переросла vmagent-овый maxScrapeSize, все
    kube_* исчезли из VM → снапшот отрисовал `Crashloops: None` рядом с
    trend-строкой `crashloops avg 40→35` из kg_cluster_observations, то есть
    дайджест противоречил сам себе в двух соседних строках.
    """
    if value is None:
        return "?"
    if isinstance(value, float):
        return f"{value:.0f}"
    return str(value)


def health_marker(score: float) -> str:
    """Цветовой маркер для health_score: 🟢 ≥0.7, 🟡 0.4-0.7, 🔴 <0.4."""
    if score >= 0.7:
        return "🟢"
    if score >= 0.4:
        return "🟡"
    return "🔴"


def humanize_ago(iso_str: Optional[str], now: Optional[datetime] = None) -> str:
    """ISO timestamp → '5m ago' / '2h ago' / '3d ago'."""
    if not iso_str:
        return "?"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    now = now or datetime.now(timezone.utc)
    delta = now - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def format_services_list(names: List[str], cap: int = 3) -> str:
    """`['town-service', 'chat-tasks-service', 'map-service', 'a', 'b']`
    → `'town-service/chat-tasks-service/map-service +2 more'`."""
    if not names:
        return "?"
    shown = names[:cap]
    rest = len(names) - cap
    base = "/".join(shown)
    if rest > 0:
        base += f" +{rest} more"
    return base


def format_gap_minutes(lag_min: float) -> str:
    """`5m` / `90m` → `1h` / `2h` (часы при >= 60m). Используется gauge'ами."""
    hours = lag_min / 60
    if hours >= 1:
        return f"{hours:.0f}h"
    return f"{lag_min:.0f}m"
