"""Дайджест должен сообщать, что часть его секций не собралась.

Мотивация — инцидент 07.08.2026. Секция ловит исключение и возвращает "",
поэтому «блок упал» и «по блоку нет данных» выглядят одинаково: из дайджеста
молча пропали deploy→incident и beat_heartbeats, а обнаружено это было
глазами читателя, а не мониторингом.

Здесь проверяется, что падение секции оставляет след: имя секции попадает в
трекер, а трекер рендерится отдельной строкой дайджеста.
"""
from __future__ import annotations

from app.services import stats_digest as sd


def test_tx_clean_records_calling_section():
    """_tx_clean запоминает, ИЗ КАКОЙ секции его позвали."""
    sd._reset_section_failures()

    def cluster_health_section():  # имя важно — оно и попадёт в трекер
        sd._tx_clean(None)

    cluster_health_section()

    line = sd.section_failures_line()
    assert "cluster_health_section" in line
    assert "Секции недоступны (1)" in line


def test_failures_line_empty_when_all_ok():
    """Ничего не упало — строки нет (не мусорим в дайджесте)."""
    sd._reset_section_failures()
    assert sd.section_failures_line() == ""


def test_failures_deduplicated_and_sorted():
    """Повторный сбой той же секции не удваивает запись."""
    sd._reset_section_failures()

    def mttr_section():
        sd._tx_clean(None)

    def anomaly_summary_section():
        sd._tx_clean(None)

    mttr_section()
    mttr_section()
    anomaly_summary_section()

    line = sd.section_failures_line()
    assert "Секции недоступны (2)" in line
    # порядок стабильный (sorted) — иначе дайджест шумит диффом между днями
    assert line.index("anomaly_summary_section") < line.index("mttr_section")


def test_reset_isolates_consecutive_builds():
    """Новая сборка дайджеста не наследует сбои предыдущей."""
    sd._reset_section_failures()

    def kg_quality_section():
        sd._tx_clean(None)

    kg_quality_section()
    assert sd.section_failures_line() != ""

    sd._reset_section_failures()
    assert sd.section_failures_line() == ""
