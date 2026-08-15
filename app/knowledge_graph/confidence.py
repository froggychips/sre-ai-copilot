"""G5+G2.2: confidence-score formula для edges.

Принимает (extras, last_seen_at) edge → returns [0, 1].

base = 0.5 (inferred-env baseline, что-либо без подтверждения)
× source multiplier (0/1/2/3+ источников)
× freshness multiplier (по last_seen_at)
clamp [0, 1]

Label thresholds:
  score ≥ 0.7 → "high"
  0.4 ≤ score < 0.7 → "medium"
  score < 0.4 → "low"

Используется в queries.upstream_of для дополнения dict-ответа и в
discord_service для badge в embed (●●●/●●○/●○○).

Будущие runtime-источники (OTEL / VM metrics) могут передавать
discovered_by="kg_sync/runtime_seen" — это добавит источник + поднимет
freshness, что даст конкретный edge ближе к "high".
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional


# Source precedence (per ChatGPT review #3.1): explicit truth hierarchy.
# Без неё все inferred-источники одинаковые base=0.5. Теперь сила source-а
# зависит от того, насколько он близко к runtime-наблюдению:
#   runtime traces (OTEL spans, VM client metrics) > declared (k8s Ingress) >
#   strong inferred (DSN из secret-key) > weak inferred (env-vars).
# При множественных источниках берётся MAX precedence + bonus за corroboration.
_SOURCE_PRECEDENCE: Dict[str, float] = {
    # Tier 1: runtime-observed (1.0). Не реализовано пока — placeholder для
    # будущих OTEL / VM metrics источников.
    "kg_sync/otel_runtime": 1.0,
    "kg_sync/vm_runtime": 1.0,
    "kg_sync/runtime_seen": 1.0,    # зарезервировано под L7-источники
    "kg_sync/runtime_corr": 0.95,   # PodEvent ↔ ServiceEdge correlation
    # Endpoints — фактическое состояние кластера, а не манифест: контроллер
    # записал адреса РЕАЛЬНО готовых подов. Сильнее объявления (0.85), но
    # слабее наблюдённого вызова: «поды есть» ещё не значит «к ним ходят».
    "k8s_endpoints/ready": 0.90,
    # Tier 2: declarative k8s resources (0.85). Manifest существует физически.
    "kg_sync/ingress": 0.85,
    "kg_sync/service": 0.85,
    "kg_sync/network_policy": 0.85,
    # Те же declarative-источники под именами, которые РЕАЛЬНО пишет
    # k8s_topology_resources_sync (DISCOVERED_BY_SVC / DISCOVERED_BY_INGRESS).
    # Префикс здесь разъехался с таблицей, и до 15.08.2026 эти источники были
    # ей неизвестны: 7209 рёбер (5547 serves_traffic + 1662 routes_to)
    # получали default 0.40 — то есть k8s-манифест, прочитанный напрямую,
    # оценивался НИЖЕ хоста, угаданного по имени секрета (0.65).
    # Граф считал догадку достовернее наблюдения; тест
    # `test_every_producer_source_is_known` не даёт этому повториться.
    "k8s_topology_resources/service": 0.85,
    "k8s_topology_resources/ingress": 0.85,
    # Job/CronJob и storage — тоже прочитанные k8s-манифесты.
    "k8s_jobs_sync/job": 0.85,
    "k8s_jobs_sync/cronjob": 0.85,
    "k8s_storage/pod_volumes": 0.85,
    "k8s_storage/pvc_spec": 0.85,
    # Tier 3: strong inference (0.65). Имя secret key явно говорит про DB.
    "kg_sync/secret_hint": 0.65,
    "kg_sync/dsn_env": 0.65,
    # Парсер исходников монорепы: код прямо называет subject, который сервис
    # публикует. Сильный вывод, но всё же объявление, а не наблюдение вызова.
    "kg_sync/nats_subjects_parser": 0.65,
    # Tier 4: weak inference (0.50). Env-vars с URL/HOST в имени.
    "kg_sync/env_url_v2": 0.50,
    "kg_sync/env_vars": 0.50,
    "kg_sync/nats_env": 0.50,
}
_SOURCE_PRECEDENCE_DEFAULT = 0.40  # unknown sources — ниже weakest known


def _source_precedence_max(sources: list) -> float:
    """MAX precedence среди источников. Reflects «what's the strongest
    discovery method for this edge» — runtime > k8s manifest > inference."""
    if not sources:
        return 0.0
    return max(_SOURCE_PRECEDENCE.get(s, _SOURCE_PRECEDENCE_DEFAULT) for s in sources)


def confidence_score(
    extras: Optional[Dict[str, Any]],
    last_seen_at: Optional[datetime],
) -> float:
    """Calculate edge confidence [0, 1] from precedence + corroboration + freshness.

    Formula (revised per ChatGPT review #3.1):
        precedence = max_source_weight  (runtime 1.0 → manifest 0.85 → secret 0.65 → env 0.5)
        corroboration = +0.10 per additional unique source (capped +0.20)
        freshness = decay multiplier по last_seen_at
        score = (precedence + corroboration) × freshness, clamped [0, 1]

    Это даёт явную precedence model вместо равной base=0.5 для всех inferred.
    Runtime-observed edge получает score ≈ 1.0 (●●●) сразу. Env-only edge
    остаётся ●●○ medium даже если fresh — потому что precedence низкая.
    """
    sources = (extras or {}).get("discovery_sources") or []
    if not sources:
        # Backfill-эпохальные edges без provenance — нулевая уверенность.
        return 0.0

    precedence = _source_precedence_max(sources)
    unique_sources = len(set(sources))
    corroboration = min(0.20, max(0, unique_sources - 1) * 0.10)

    if last_seen_at is None:
        fresh_mul = 0.5
    else:
        age_sec = (datetime.utcnow() - last_seen_at).total_seconds()
        age_days = age_sec / 86400.0
        if age_days < 1 / 24:        # < 1 час
            fresh_mul = 1.0
        elif age_days < 1:           # < 1 день
            fresh_mul = 0.95
        elif age_days < 7:           # < неделя
            fresh_mul = 0.8
        elif age_days < 30:          # < месяц
            fresh_mul = 0.5
        else:                        # > месяца — stale
            fresh_mul = 0.2

    return min(1.0, (precedence + corroboration) * fresh_mul)


def confidence_label(score: float) -> str:
    """Map [0, 1] score → human-readable bucket."""
    if score >= 0.7:
        return "high"
    if score >= 0.4:
        return "medium"
    return "low"


def confidence_badge(score: float) -> str:
    """Visual badge для Discord embed: ●●● / ●●○ / ●○○."""
    label = confidence_label(score)
    return {"high": "●●●", "medium": "●●○", "low": "●○○"}.get(label, "○○○")
