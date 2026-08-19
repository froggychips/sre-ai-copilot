"""Эпистемический статус: откуда мы знаем факт и не спорит ли с ним другой.

К августу 2026 в графе набралось семь параллельных осей уверенности —
OWNER_SOURCE_TRUST, _SOURCE_PRECEDENCE, stale_class, synthetic, node_kind,
состояние namespace, QUALITY_THRESHOLDS. Общего словаря не было, и простой
вопрос «наблюдали или вывели» задать было некому.

Главное новое — CONTRADICTED. Граф умел «знаю» и «не знаю», но не умел
«источники утверждают разное», хотя такие случаи на живых данных не редкость
(замер 19.08.2026):

  * 98 рёбер serves_traffic, где топология говорит «обслуживает», а endpoints
    — «ноль готовых подов»;
  * 2192 узла обновляются в namespace, помеченных missing.

До модели такие расхождения выглядели обычными фактами — то есть худшим
видом неправды: уверенным ответом из двух несогласных источников.
"""
from datetime import datetime, timedelta

import pytest

from app.knowledge_graph.epistemic import (EPISTEMIC_BADGE, EPISTEMIC_WEIGHT,
                                           Epistemic, classify_edge,
                                           find_edge_contradictions)

NOW = datetime(2026, 8, 19, 12, 0)
FRESH = NOW - timedelta(hours=1)


def _verdict(sources, seen=FRESH, **kw):
    return classify_edge(sources, seen, now=NOW, **kw)


# --- шкала ----------------------------------------------------------------


def test_observation_outranks_declaration():
    """Манифест описывает намерение, наблюдение — факт."""
    assert (_verdict(["k8s_endpoints/ready"]).weight
            > _verdict(["kg_sync/ingress"]).weight)


def test_declaration_outranks_inference():
    assert (_verdict(["kg_sync/ingress"]).weight
            > _verdict(["kg_sync/secret_hint"]).weight)


def test_agreement_of_weak_sources_beats_a_single_one():
    """Два независимых слабых источника сильнее одного слабого."""
    assert (_verdict(["kg_sync/env_vars", "kg_sync/env_url_v2"]).status
            is Epistemic.CORROBORATED)
    assert (_verdict(["kg_sync/env_vars", "kg_sync/env_url_v2"]).weight
            > _verdict(["kg_sync/env_vars"]).weight)


def test_contradiction_ranks_below_a_plain_guess():
    """Догадка честно называет себя догадкой; противоречие выглядит фактом.

    Поэтому CONTRADICTED весит меньше INFERRED — пока конфликт не назван, он
    опаснее открытой неуверенности.
    """
    conflict = _verdict(["k8s_topology_resources/service"],
                        contradictions=["источники спорят"])
    assert conflict.weight < _verdict(["kg_sync/secret_hint"]).weight


# --- CONTRADICTED ---------------------------------------------------------


def test_contradiction_overrides_any_number_of_sources():
    """Сколько бы источников ни подтверждало — опровержение важнее."""
    v = _verdict(
        ["k8s_endpoints/ready", "kg_sync/ingress", "kg_sync/service"],
        contradictions=["endpoints говорит обратное"],
    )
    assert v.status is Epistemic.CONTRADICTED


def test_contradiction_is_not_actionable():
    """Противоречие — вопрос без ответа, а не слабый факт."""
    v = _verdict(["kg_sync/ingress"], contradictions=["спор"])
    assert not v.is_actionable
    assert _verdict(["kg_sync/ingress"]).is_actionable


def test_contradiction_carries_the_explanation():
    """Показать конфликт честнее, чем выбрать сторону за пользователя."""
    v = _verdict(["kg_sync/ingress"], contradictions=["A против B"])
    assert v.conflicts == ["A против B"]
    assert v.badge == "⚠"


# --- поиск конфликтов на реальных случаях ---------------------------------


def test_serves_traffic_without_ready_pods_is_a_contradiction():
    """Случай с прода: 98 таких рёбер на 19.08.2026."""
    found = find_edge_contradictions(
        kind="serves_traffic", src_metadata={"endpoints_ready": 0}
    )
    assert found and "endpoints" in found[0]


def test_serves_traffic_with_ready_pods_is_fine():
    assert not find_edge_contradictions(
        kind="serves_traffic", src_metadata={"endpoints_ready": 3}
    )


def test_unknown_endpoints_state_is_not_a_contradiction():
    """Нет данных — не то же самое, что данные против."""
    assert not find_edge_contradictions(kind="serves_traffic", src_metadata={})


def test_updates_in_a_missing_namespace_are_a_contradiction():
    """Случай с прода: 2192 таких узла."""
    found = find_edge_contradictions(kind="calls", namespace_state="missing")
    assert found and "исчезнувш" in found[0]


def test_other_edge_kinds_are_not_touched_by_endpoints_rule():
    """Правило про подов относится только к serves_traffic."""
    assert not find_edge_contradictions(
        kind="uses_db", src_metadata={"endpoints_ready": 0}
    )


# --- устаревание ----------------------------------------------------------


def test_old_observation_becomes_stale():
    """Наблюдение месячной давности — уже воспоминание, а не наблюдение."""
    v = classify_edge(["k8s_endpoints/ready"], NOW - timedelta(days=30), now=NOW)
    assert v.status is Epistemic.STALE


def test_staleness_is_checked_before_source_strength():
    """Иначе сильный источник маскировал бы просроченность факта."""
    v = classify_edge(["kg_sync/runtime_seen"], NOW - timedelta(days=60), now=NOW)
    assert v.status is Epistemic.STALE


# --- отсутствие данных ----------------------------------------------------


@pytest.mark.parametrize("sources", [None, [], [""]])
def test_no_provenance_is_unknown_not_safe(sources):
    """«Не знаю» обязано быть худшим вариантом, а не средним."""
    v = _verdict(sources)
    assert v.status is Epistemic.UNKNOWN
    assert v.weight == 0.0
    assert not v.is_actionable


def test_every_status_has_weight_and_badge():
    """Иначе потребитель напишет свой маппинг — и копии разъедутся."""
    for status in Epistemic:
        assert status in EPISTEMIC_WEIGHT
        assert status in EPISTEMIC_BADGE


def test_verdict_always_explains_itself():
    """«Почему граф так считает» спрашивают чаще, чем сам статус."""
    for sources in (["k8s_endpoints/ready"], ["kg_sync/secret_hint"], []):
        assert _verdict(sources).reasons
