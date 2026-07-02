"""Тесты на teamcity_service.

Покрывают только pure-функции (фильтры, парсеры), без сетевых интеграций.
"""
from datetime import datetime, timedelta, timezone

from app.services.teamcity_service import (
    _TC_CLIENT_AVAILABLE,
    _TC_CLIENT_SOURCE,
    _build_summary_direct,
    _is_deploy_buildtype_name,
    _parse_tc_date,
    _tc_to_iso,
)


# ── TC client availability (vendor fallback) ────────────────────────────────


def test_tc_client_is_available_via_vendor_in_test_env():
    """В контейнере / CI пакет teamcity-mcp не установлен через pip, и
    TEAMCITY_MCP_URL пустой. Без vendor-фолбэка direct TC REST не работал.

    Тест гарантирует что:
      1. После переезда на vendor — _TC_CLIENT_AVAILABLE=True всегда.
      2. _TC_CLIENT_SOURCE сообщает откуда взят клиент: 'external' (pip /
         TC_MCP_SRC) или 'vendor' (app.vendor.teamcity_mcp).
    """
    assert _TC_CLIENT_AVAILABLE is True
    assert _TC_CLIENT_SOURCE in ("external", "vendor")


def test_is_deploy_buildtype_name_accepts_typical_names():
    assert _is_deploy_buildtype_name("Build and update")
    assert _is_deploy_buildtype_name("Build and full deploy")
    assert _is_deploy_buildtype_name("Kingdom deploy")
    assert _is_deploy_buildtype_name("Shared deploy")
    assert _is_deploy_buildtype_name("Backup all db")
    assert _is_deploy_buildtype_name("Build and update service")


def test_is_deploy_buildtype_name_rejects_non_deploy_buildtypes():
    """Custom WO-builds которые содержат deploy/update в name, но не катят код."""
    assert not _is_deploy_buildtype_name("Set client min version")
    assert not _is_deploy_buildtype_name("Set ab test")
    assert not _is_deploy_buildtype_name("Update terrain")
    assert not _is_deploy_buildtype_name("Update secret")
    assert not _is_deploy_buildtype_name("Delete namespace")


def test_is_deploy_buildtype_name_handles_none_and_empty():
    assert not _is_deploy_buildtype_name(None)
    assert not _is_deploy_buildtype_name("")


def test_is_deploy_buildtype_name_is_case_insensitive():
    assert _is_deploy_buildtype_name("BACKUP ALL DB")
    assert _is_deploy_buildtype_name("kingdom DEPLOY")


# ── _parse_tc_date: compact + ISO fallback (regression WO deploy-attribution) ─


def test_parse_tc_date_accepts_compact_tc_format():
    """Сырой формат TC REST: yyyyMMdd'T'HHmmss±HHmm без двоеточий."""
    dt = _parse_tc_date("20260512T174418+0000")
    assert dt is not None
    assert dt.tzinfo is not None  # tz-aware
    assert dt == datetime(2026, 5, 12, 17, 44, 18, tzinfo=timezone.utc)


def test_parse_tc_date_accepts_compact_tc_with_offset():
    """Ненулевой offset должен корректно нормализоваться к UTC."""
    dt = _parse_tc_date("20260512T204418+0300")
    assert dt is not None
    assert dt.astimezone(timezone.utc) == datetime(
        2026, 5, 12, 17, 44, 18, tzinfo=timezone.utc
    )


def test_parse_tc_date_accepts_iso_with_z():
    """ISO 8601 c Z — то что кладёт _tc_to_iso в поле `finished`.

    Это и есть корень бага: merge/filter re-парсят уже-ISO значение.
    """
    dt = _parse_tc_date("2026-05-12T17:44:18Z")
    assert dt is not None
    assert dt.tzinfo is not None  # tz-aware, иначе сравнение с `since` падает
    assert dt == datetime(2026, 5, 12, 17, 44, 18, tzinfo=timezone.utc)


def test_parse_tc_date_accepts_iso_with_offset():
    dt = _parse_tc_date("2026-05-12T17:44:18+00:00")
    assert dt is not None
    assert dt == datetime(2026, 5, 12, 17, 44, 18, tzinfo=timezone.utc)


def test_parse_tc_date_naive_iso_treated_as_utc():
    """ISO без tz не должен ломать сравнение с tz-aware `since`."""
    dt = _parse_tc_date("2026-05-12T17:44:18")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt == datetime(2026, 5, 12, 17, 44, 18, tzinfo=timezone.utc)


def test_parse_tc_date_rejects_garbage():
    assert _parse_tc_date("") is None
    assert _parse_tc_date(None) is None  # type: ignore[arg-type]
    assert _parse_tc_date("not-a-date") is None
    assert _parse_tc_date("2026/05/12 17:44") is None


def test_tc_to_iso_compact_unchanged_after_fix():
    """No-regression: сырой compact TC нормализуется в ISO как и до фикса."""
    assert _tc_to_iso("20260512T174418+0000") == "2026-05-12T17:44:18Z"
    # ненулевой offset тоже сводится к UTC-Z
    assert _tc_to_iso("20260512T204418+0300") == "2026-05-12T17:44:18Z"
    assert _tc_to_iso(None) is None
    # нераспознанный формат возвращается как есть
    assert _tc_to_iso("garbage") == "garbage"


# ── регрессия: build из _build_summary_direct НЕ дропается merge/filter ───────


def test_build_summary_finished_survives_since_filter():
    """Корень бага priority#2: _build_summary_direct кладёт finished в ISO,
    merge/filter re-парсят его через _parse_tc_date. До фикса ISO не парсился →
    finished=None → build дропался → «recent deploys» всегда пустой.

    Здесь воспроизводим фильтр 1:1: строим summary из compact-даты, затем
    re-парсим `finished` и проверяем что он попадает в окно `since`.
    """
    b = _build_summary_direct(
        {
            "id": 12345,
            "number": "42",
            "status": "SUCCESS",
            "state": "finished",
            "buildTypeId": "Wo_Backend_BuildAndUpdate",
            "branchName": "refs/heads/prod",
            "startDate": "20260512T174000+0000",
            "finishDate": "20260512T174418+0000",
        }
    )
    # summary хранит finished уже в ISO
    assert b["finished"] == "2026-05-12T17:44:18Z"

    # merge/filter логика: re-парс + сравнение с since
    finished = _parse_tc_date(b.get("finished", ""))
    assert finished is not None  # <-- до фикса было None (build дропался)

    since = datetime(2026, 5, 12, 17, 44, 18, tzinfo=timezone.utc) - timedelta(minutes=60)
    assert finished >= since  # build в окне → удерживается


def test_build_summary_finished_dropped_when_older_than_since():
    """Sanity: за пределами окна build корректно отсеивается (не всегда True)."""
    b = _build_summary_direct(
        {
            "id": 999,
            "finishDate": "20260512T100000+0000",
        }
    )
    finished = _parse_tc_date(b.get("finished", ""))
    assert finished is not None
    since = datetime(2026, 5, 12, 17, 44, 18, tzinfo=timezone.utc) - timedelta(minutes=60)
    assert finished < since  # старый build → дропается
