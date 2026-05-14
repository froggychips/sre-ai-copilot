"""Тесты на teamcity_service.

Покрывают только pure-функции (фильтры, парсеры), без сетевых интеграций.
"""
from app.services.teamcity_service import (
    _TC_CLIENT_AVAILABLE,
    _TC_CLIENT_SOURCE,
    _is_deploy_buildtype_name,
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
