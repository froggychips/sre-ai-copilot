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


# ── deadman: доехал ли дайджест до Discord ──────────────────────────────────


def test_digest_delivery_check_ok_when_fresh(monkeypatch):
    """Свежий маркер доставки → ok."""
    from datetime import datetime, timedelta, timezone

    from app.knowledge_graph import self_health

    monkeypatch.setattr(self_health.settings, "STATS_DIGEST_ENABLED", True, raising=False)
    monkeypatch.setattr(
        sd, "_get_beat_last_run",
        lambda task: datetime.now(timezone.utc) - timedelta(hours=3),
    )

    r = self_health.check_digest_delivery(db=None)
    assert r.status == "ok"
    assert r.detail["last_success_age_minutes"] < 26 * 60


def test_digest_delivery_check_fails_when_day_skipped(monkeypatch):
    """Пропущенные сутки → fail (это и есть deadman)."""
    from datetime import datetime, timedelta, timezone

    from app.knowledge_graph import self_health

    monkeypatch.setattr(self_health.settings, "STATS_DIGEST_ENABLED", True, raising=False)
    monkeypatch.setattr(
        sd, "_get_beat_last_run",
        lambda task: datetime.now(timezone.utc) - timedelta(hours=30),
    )

    r = self_health.check_digest_delivery(db=None)
    assert r.status == "fail"


def test_digest_delivery_check_warns_without_marker(monkeypatch):
    """Маркера нет вовсе → warn, а не fail: мог быть рестарт redis."""
    from app.knowledge_graph import self_health

    monkeypatch.setattr(self_health.settings, "STATS_DIGEST_ENABLED", True, raising=False)
    monkeypatch.setattr(sd, "_get_beat_last_run", lambda task: None)

    r = self_health.check_digest_delivery(db=None)
    assert r.status == "warn"


def test_digest_delivery_check_skipped_when_disabled(monkeypatch):
    """Флаг выключен → ok со пометкой skipped, не ложная тревога."""
    from app.knowledge_graph import self_health

    monkeypatch.setattr(self_health.settings, "STATS_DIGEST_ENABLED", False, raising=False)

    r = self_health.check_digest_delivery(db=None)
    assert r.status == "ok"
    assert "skipped" in r.detail


def test_digest_delivery_registered_in_all_checks():
    """Чек реально включён в набор self-health, а не просто определён."""
    from app.knowledge_graph import self_health

    assert self_health.check_digest_delivery in self_health._ALL_CHECKS
