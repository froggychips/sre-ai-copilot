"""Имя ноды для алертов, которые приезжают с одним IP.

ЗАЧЕМ. У метрик node-exporter метки `node` нет, а `instance` — это IP ПОДА
(DaemonSet не в hostNetwork). В Discord приходило «NodeSystemSaturation ·
192.168.74.165:9100», и по такому сообщению непонятно, о какой ноде речь: имя
приходилось выяснять руками через `kube_pod_info`.

ПОЧЕМУ НЕ ТОЛЬКО В ПРАВИЛАХ. Часть нодовых алертов — наши VMRule, там связка
уже вшита в expr (`node:node_name_info`, VMRule node-name-info). Но
`NodeSystemSaturation`, `NodeCPUHighUsage`, `NodeMemoryHighUtilization`,
`NodeFilesystem*`, `NodeClockNotSynchronising` и остальная группа
`node-exporter` приезжают ИЗ ЧАРТА victoria-metrics-k8s-stack — их expr мы не
контролируем, и чтобы дописать туда join, пришлось бы форкнуть и тащить у себя
всю группу. Резолв на стороне копилота закрывает их все разом и переживает
апгрейд чарта.

СВЯЗКА. По метке `pod`: `vm-node-exporter-XXXXX` есть и у node-exporter, и у
kube_pod_info. Метка `instance` для этого НЕ годится — у `kube_node_info` в
`internal_ip` лежит ВНЕШНИЙ адрес ноды (у dev-14 — 195.154.249.183), он
никогда не совпадёт с IP пода.

КЭШ. Поды DaemonSet'а переезжают редко, а alert storm приносит десятки алертов
за секунды — карта тянется раз в NODE_RESOLVE_CACHE_TTL_SEC на весь процесс.
Промах кэша тоже кэшируется по времени: при мёртвой VM мы не должны долбить её
на каждый алерт.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Dict, Optional

from app.config import settings
from app.context.vm_client import VMClient

logger = logging.getLogger(__name__)

# Под node-exporter'а: `vm-node-exporter-gbn9k`. Резолвим только для них —
# у остальных алертов либо метка `node` уже есть, либо `instance` осмысленный.
_EXPORTER_POD_RE = re.compile(r"^[a-z0-9-]*node-exporter-[a-z0-9]+$")

_CACHE_TTL_SEC = 600.0

_cache: Dict[str, str] = {}
_cache_at: float = 0.0
_lock = asyncio.Lock()


def is_node_exporter_pod(pod: Optional[str]) -> bool:
    """True для пода node-exporter'а — только их имеет смысл резолвить."""
    return bool(pod) and bool(_EXPORTER_POD_RE.match(pod or ""))


def reset_cache() -> None:
    """Сбросить карту (тесты; смена состава нод в рантайме ждёт TTL)."""
    global _cache, _cache_at
    _cache = {}
    _cache_at = 0.0


async def _node_map() -> Dict[str, str]:
    """Карта pod → node, не чаще раза в TTL.

    Лок держится на время запроса, поэтому alert storm из N алертов даёт ОДИН
    поход в VM, а не N: остальные ждут на локе и читают уже прогретый кэш.
    """
    global _cache, _cache_at
    async with _lock:
        if _cache_at and (time.monotonic() - _cache_at) < _CACHE_TTL_SEC:
            return _cache
        vm = VMClient(settings.VICTORIA_METRICS_URL, timeout=5.0)
        try:
            fresh = await vm.resolve_node_names()
        except Exception as e:
            # Резолв — украшение, а не условие доставки: молчим и оставляем IP.
            logger.debug("node_resolver: карта нод недоступна: %s", e)
            fresh = {}
        # Пустой ответ кэшируем ТОЖЕ (защита от долбёжки мёртвой VM), но старую
        # карту не затираем: имя ноды из прошлого тика точнее, чем никакого.
        _cache_at = time.monotonic()
        if fresh:
            _cache = fresh
        return _cache


async def resolve_node_name(labels: Dict[str, str]) -> Optional[str]:
    """Имя ноды для алерта, либо None если резолвить нечего/не вышло.

    None — штатный исход: алерт уходит с `instance`, как уходил раньше.
    """
    if not settings.NODE_NAME_RESOLVE_ENABLED or not settings.VICTORIA_METRICS_URL:
        return None
    if not labels or labels.get("node"):
        return None
    pod = labels.get("pod")
    if not is_node_exporter_pod(pod):
        return None
    return (await _node_map()).get(pod or "")


async def annotate_node_label(labels: Dict[str, str]) -> Optional[str]:
    """Дописать в labels метку `node` (in-place). Возвращает имя ноды либо None.

    Метка кладётся в сами labels инцидента, чтобы имя ноды видели ВСЕ
    потребители — рендер embed'а, kg_alerts, дайджест — а не только тот, кто
    позвал резолвер.
    """
    node = await resolve_node_name(labels)
    if node:
        labels["node"] = node
    return node
