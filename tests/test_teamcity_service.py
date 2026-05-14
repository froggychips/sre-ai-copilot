"""Тесты на teamcity_service.

Покрывают только pure-функции (фильтры, парсеры), без сетевых интеграций.
"""
from app.services.teamcity_service import _is_deploy_buildtype_name


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
