"""Общее для правил «контейнер не стартовал»: image_pull и container_config.

Оба класса — про под, который до запуска процесса не дошёл: образ не
вытянулся или kubelet не собрал окружение контейнера из Secret/ConfigMap.
Сигналы у них одинаковой природы — k8s Events (`Failed`, `BackOff`,
`ErrImagePull`, `CreateContainerConfigError`) и waiting-state контейнера в
тексте снапшота, — поэтому и привязка события к target-workload у них общая,
та же, что у PodEventsRule: чужой workload namespace-а — не наблюдение про
этот инцидент.

`is_image_pull_backoff` (живёт в pod_events.py) нужен и crashloop-правилам: kubelet пишет
«Back-off pulling image» под тем же reason `BackOff`, что и
«Back-off restarting failed container», и раньше такой под числился в
crashloop — хотя процесс в нём ни разу не запускался.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.diagnostics.rules.pod_events import (_attribution, _event_object,
                                              is_image_pull_backoff)

SCOPED = "scoped"
FOREIGN = "foreign"
UNVERIFIED = "unverified"

# Во сколько раз режется confidence события без проверяемой привязки —
# ровно как в PodEventsRule: в soft-зону fact_critic, не в anchor.
UNVERIFIED_CONFIDENCE_FACTOR = 0.5


def classify_events(
    events: List[Dict[str, Any]], target: Optional[str],
) -> List[Tuple[str, Dict[str, Any]]]:
    """[(класс привязки, событие)] — только dict-события."""
    out: List[Tuple[str, Dict[str, Any]]] = []
    for ev in events:
        if isinstance(ev, dict):
            out.append((_attribution(_event_object(ev), target), ev))
    return out


def event_object(ev: Dict[str, Any]) -> str:
    return _event_object(ev)


__all__ = [
    "FOREIGN", "SCOPED", "UNVERIFIED", "UNVERIFIED_CONFIDENCE_FACTOR",
    "classify_events", "event_object", "is_image_pull_backoff",
]
