"""Тесты на teamcity_service.

Покрывают pure-функции (фильтры, парсеры) и пагинацию TC REST на моке
клиента — без реальной сети.
"""
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.services.teamcity_service import (
    _TC_CLIENT_AVAILABLE,
    _TC_CLIENT_SOURCE,
    _TC_MAX_PAGES,
    _TC_PAGE_SIZE,
    _build_summary_direct,
    _fetch_recent_deploys_direct,
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


# ── пагинация recent_deploys (находка второй волны кодревью) ─────────────────
#
# Раньше был один запрос `count:200`: TC отдавал 200 САМЫХ СВЕЖИХ билдов
# проекта (включая не-deploy), и только потом Python-фильтр по имени buildType
# выкидывал лишнее. Кап съедали чужие билды, а
# `backfill_tc_deploys --days 30 --limit 1000` молча получал ≤200 сырых билдов
# на проект и терял хвост истории.

_BASE_FINISH = datetime(2026, 8, 10, 18, 0, 0, tzinfo=timezone.utc)


def _tc_build(idx: int, buildtype_name: str) -> dict:
    """Синтетический build из TC REST: чем больше idx, тем он старше."""
    finished = _BASE_FINISH - timedelta(minutes=idx)
    stamp = finished.strftime("%Y%m%dT%H%M%S+0000")
    return {
        "id": 900000 + idx,
        "number": str(1000 + idx),
        "status": "SUCCESS",
        "state": "finished",
        "branchName": "refs/heads/prod",
        "buildTypeId": f"Wo_Backend_Bt{idx}",
        "buildType": {"name": buildtype_name},
        "startDate": stamp,
        "finishDate": stamp,
        "triggered": {"type": "user", "user": {"username": "yar"}},
        "revisions": {
            "revision": [
                {
                    "version": f"{idx:040d}",
                    "vcs-root-instance": {"name": "wo-backend", "vcs-root-id": "Wo_Root"},
                }
            ]
        },
    }


class _FakeTCClient:
    """Мок TeamCityClient: режет заготовленный список по start/count локатора."""

    def __init__(self, builds: list, fail_pages=()):
        self.builds = builds
        self.fail_pages = set(fail_pages)
        self.locators: list[str] = []
        self.closed = 0

    def get_json(self, path, params=None):
        locator = (params or {})["locator"]
        self.locators.append(locator)
        count = int(re.search(r"count:(\d+)", locator).group(1))
        start = int(re.search(r"start:(\d+)", locator).group(1))
        page = start // count
        if page in self.fail_pages:
            raise RuntimeError(f"TC 502 on page {page}")
        return {"build": self.builds[start:start + count]}

    def close(self):
        self.closed += 1

    @property
    def pages_requested(self) -> int:
        return len(self.locators)


def _fetch(fake: _FakeTCClient, limit: int = 1000):
    with patch("app.services.teamcity_service._TCClient", new=lambda **kw: fake):
        return _fetch_recent_deploys_direct("Wo_Backend", _BASE_FINISH - timedelta(days=30), limit)


def test_recent_deploys_pagination_reaches_builds_behind_the_cap():
    """200 не-deploy билдов свежее всех deploy-ов: до фикса результат был пуст."""
    builds = [_tc_build(i, "Set ab test") for i in range(_TC_PAGE_SIZE)]
    builds += [_tc_build(_TC_PAGE_SIZE + i, "Kingdom deploy") for i in range(10)]
    fake = _FakeTCClient(builds)

    out = _fetch(fake)

    assert len(out) == 10
    assert {b["buildtype_name"] for b in out} == {"Kingdom deploy"}
    assert fake.pages_requested == 2
    assert f"count:{_TC_PAGE_SIZE},start:0" in fake.locators[0]
    assert f"count:{_TC_PAGE_SIZE},start:{_TC_PAGE_SIZE}" in fake.locators[1]
    assert fake.closed == 1


def test_recent_deploys_single_short_page_is_one_request():
    """Горячий путь (дайджест, окно 24ч): страница неполная → один запрос."""
    fake = _FakeTCClient([_tc_build(i, "Kingdom deploy") for i in range(50)])

    out = _fetch(fake)

    assert len(out) == 50
    assert fake.pages_requested == 1


def test_recent_deploys_page_cap_is_logged_not_silent():
    """Кап страниц = обрезанная история; об этом обязан быть warning."""
    builds = [_tc_build(i, "Kingdom deploy") for i in range(_TC_PAGE_SIZE * (_TC_MAX_PAGES + 2))]
    fake = _FakeTCClient(builds)
    fake_logger = MagicMock()

    with patch("app.services.teamcity_service.logger", new=fake_logger):
        out = _fetch(fake, limit=5000)

    assert fake.pages_requested == _TC_MAX_PAGES
    assert len(out) == _TC_PAGE_SIZE * _TC_MAX_PAGES  # кап, а не все 2400
    events = [c.args[0] for c in fake_logger.warning.call_args_list]
    assert "teamcity.recent_deploys_page_cap_reached" in events
    kwargs = fake_logger.warning.call_args.kwargs
    assert kwargs["pages"] == _TC_MAX_PAGES
    assert kwargs["scanned"] == _TC_PAGE_SIZE * _TC_MAX_PAGES


def test_recent_deploys_keeps_partial_result_when_later_page_fails():
    """Отказ 2-й страницы не должен обнулять уже собранные деплои."""
    builds = [_tc_build(i, "Kingdom deploy") for i in range(_TC_PAGE_SIZE * 3)]
    fake = _FakeTCClient(builds, fail_pages=(1,))
    fake_logger = MagicMock()

    with patch("app.services.teamcity_service.logger", new=fake_logger):
        out = _fetch(fake)

    assert len(out) == _TC_PAGE_SIZE
    events = [c.args[0] for c in fake_logger.warning.call_args_list]
    assert "teamcity.recent_deploys_page_failed" in events
    assert fake.closed == 1


def test_recent_deploys_first_page_failure_propagates():
    """Первая страница = отказ проекта; recent_deploys ловит его выше и
    продолжает с остальными projects."""
    fake = _FakeTCClient([_tc_build(0, "Kingdom deploy")], fail_pages=(0,))

    with pytest.raises(RuntimeError):
        _fetch(fake)
    assert fake.closed == 1  # клиент закрыт даже на исключении


def test_recent_deploys_limit_applied_to_newest_after_filtering():
    """limit режет уже отфильтрованный список, отсортированный по finished DESC."""
    builds = []
    for i in range(_TC_PAGE_SIZE + 20):
        builds.append(_tc_build(i, "Kingdom deploy" if i % 2 == 0 else "Update terrain"))
    fake = _FakeTCClient(builds)

    out = _fetch(fake, limit=5)

    assert len(out) == 5
    assert [b["number"] for b in out] == ["1000", "1002", "1004", "1006", "1008"]
    finished = [b["finished_at"] for b in out]
    assert finished == sorted(finished, reverse=True)
    assert out[0]["sha"] == f"{0:040d}"
    assert out[0]["triggered_by"] == "yar"
