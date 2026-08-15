"""Каждый источник рёбер должен быть известен таблице доверия.

Прецедент 15.08.2026. `_SOURCE_PRECEDENCE` знала `kg_sync/service` и
`kg_sync/ingress`, а `k8s_topology_resources_sync` писал
`k8s_topology_resources/service` и `.../ingress` — префикс разъехался.
Неизвестный источник получает `_SOURCE_PRECEDENCE_DEFAULT` = 0.40, и на живом
графе вышло так:

    k8s_topology_resources/service   5547 рёбер → 0.40  (прочитано из k8s)
    k8s_topology_resources/ingress   1662 рёбер → 0.40  (прочитано из k8s)
    kg_sync/secret_hint              5648 рёбер → 0.65  (УГАДАНО по имени)

**Граф считал догадку достовернее наблюдения** — 7209 рёбер были занижены,
и заметить это по коду было нельзя: обе строки выглядят правдоподобно, а
рассинхрон проявляется только в данных.

Это тот же класс ошибки, что уже ловили дважды: имя UNIQUE-констрейнта в двух
`ON CONFLICT` и версия в четырёх местах. Здесь тест закрывает его на входе.
"""
import pathlib
import re

import pytest

from app.knowledge_graph.confidence import (_SOURCE_PRECEDENCE,
                                            _SOURCE_PRECEDENCE_DEFAULT,
                                            _source_precedence_max)

APP = pathlib.Path(__file__).parent.parent.parent / "app"


def _sources_written_by_code() -> set:
    """Все значения `discovered_by`, которые продюсеры реально пишут.

    Два способа: константа `DISCOVERED_BY_* = "..."` и литерал прямо в
    вызове. f-строки (`f"kg_sync/{source}"`) пропускаем — их значения
    собираются в рантайме, они покрыты отдельным тестом ниже.
    """
    found = set()
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        found |= set(re.findall(r'^DISCOVERED_BY_[A-Z_]+ = "([^"]+)"', text, re.M))
        found |= set(re.findall(r'discovered_by="([^"{]+)"', text))
    return found


def test_every_producer_source_is_known():
    """Источник, неизвестный таблице, молча занижается до 0.40."""
    unknown = sorted(_sources_written_by_code() - set(_SOURCE_PRECEDENCE))
    assert not unknown, (
        f"источники пишутся, но неизвестны _SOURCE_PRECEDENCE: {unknown}. "
        f"Они получат {_SOURCE_PRECEDENCE_DEFAULT} — ниже, чем догадка по "
        "имени секрета (0.65). Добавь их в таблицу с честным весом."
    )


def test_dynamic_kg_sync_sources_are_known():
    """`f"kg_sync/{source}"` в kg_sync подставляет dsn_env / secret_hint."""
    for source in ("kg_sync/dsn_env", "kg_sync/secret_hint"):
        assert source in _SOURCE_PRECEDENCE


# --- порядок, ради которого таблица существует ----------------------------


def test_reading_a_manifest_beats_guessing_a_name():
    """Наблюдение обязано быть выше догадки — иначе таблица бессмысленна."""
    manifest = _source_precedence_max(["k8s_topology_resources/service"])
    guess = _source_precedence_max(["kg_sync/secret_hint"])
    assert manifest > guess, (
        "прочитанный k8s-манифест не может быть менее достоверен, "
        "чем хост, угаданный по имени секрета"
    )


def test_unknown_source_ranks_below_every_known_one():
    """Дефолт — пол, а не середина: незнакомое не должно обгонять знакомое."""
    assert _SOURCE_PRECEDENCE_DEFAULT < min(_SOURCE_PRECEDENCE.values())


@pytest.mark.parametrize("declarative", [
    "k8s_topology_resources/service",
    "k8s_topology_resources/ingress",
    "k8s_jobs_sync/job",
    "k8s_jobs_sync/cronjob",
    "kg_sync/ingress",
])
def test_declarative_k8s_sources_share_one_tier(declarative):
    """Все прочитанные манифесты равны между собой: источник один — API k8s."""
    assert _SOURCE_PRECEDENCE[declarative] == 0.85


def test_runtime_outranks_declarative():
    """Наблюдённый вызов сильнее объявленного намерения."""
    assert _source_precedence_max(["kg_sync/runtime_seen"]) > \
        _source_precedence_max(["kg_sync/ingress"])
