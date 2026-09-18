"""Кто владелец стенда на витрине сквадов (WO-16030).

Дефект был не в том, что дашборд не знал владельца, а в том, что он решал
это ВТОРОЙ раз, по своей иерархии источников. Правило приоритета живёт в
графе (`app/knowledge_graph/namespace_owner.py`), и дашборду остаётся один
вопрос — что свежее: доска собирается раз в 5 минут, резолв графа идёт раз
в час.

Замер 18.09.2026: неверный владелец у четырёх стендов из 46 живых. Три из
них — ровно этот разъезд (метку `squad-owner` перекрывал assignee задачи),
четвёртый — служебный claim, проваливавшийся в мусорный источник.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "squad_dashboard.py"


@pytest.fixture(scope="module")
def dash():
    """Модуль читает TC_URL/TC_TOKEN на импорте — подставляем заглушки."""
    os.environ.setdefault("TC_URL", "https://teamcity.invalid")
    os.environ.setdefault("TC_TOKEN", "x")
    spec = importlib.util.spec_from_file_location("squad_dashboard_for_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_claim_beats_jira_assignee(dash):
    """squad-4: кнопку нажал один человек, assignee задачи — другой.

    До правки метка игнорировалась всегда, когда ветка несла WO-ключ:
    `jira_assignee` не входил в список источников, поверх которых
    разрешалось перекрывать.
    """
    owner, source = dash.pick_owner(
        {"owner_login": "aoganisyan", "owner_source": "jira_assignee"},
        {"claim_owner": "ybobryashov", "owner": "ai-agent"},
    )
    assert (owner, source) == ("ybobryashov", "gd_claim")


def test_manual_assignment_is_not_overridden(dash):
    """Ручное назначение в манифесте людей — единственное, что сильнее кнопки."""
    owner, source = dash.pick_owner(
        {"owner_login": "dgrin", "owner_source": "manual"},
        {"claim_owner": "ybobryashov"},
    )
    assert (owner, source) == ("dgrin", "manual")


def test_service_claim_is_shown_instead_of_empty_cell(dash):
    """squad-8: стенд занял агент, человека за ним нет.

    Пустая клетка читается как «ничей», хотя стенд занят, и по такой строке
    принимают решение «можно брать». Честный ответ — показать автоматику.
    """
    owner, source = dash.pick_owner(
        {"owner_login": None, "owner_source": None},
        {"claim_owner": "ai-agent"},
    )
    assert (owner, source) == ("ai-agent", "gd_claim_bot")


def test_service_claim_does_not_erase_a_known_person(dash):
    """Служебный claim не затирает человека, которого граф уже нашёл."""
    owner, source = dash.pick_owner(
        {"owner_login": "kemyashev", "owner_source": "deployed_by"},
        {"claim_owner": "ai-agent"},
    )
    assert (owner, source) == ("kemyashev", "deployed_by")


def test_label_answers_while_graph_is_silent(dash):
    """Без резолва графа отвечает лейбл — прежнее поведение, оно верное."""
    owner, source = dash.pick_owner({}, {"owner": "victor"})
    assert (owner, source) == ("victor", "label")


def test_nobody_claimed_nobody_deployed(dash):
    """Ни графа, ни лейблов — пусто, а не выдуманный владелец."""
    assert dash.pick_owner({}, {}) == (None, None)


# --- список сервисных учёток берётся оттуда же, что у графа -----------------


def test_service_accounts_come_from_the_shared_manifest(dash, tmp_path, monkeypatch):
    """Бот, дописанный в манифест, перестаёт быть человеком и для витрины.

    Замечание ревью: своя копия списка — это третье место, где решается
    «человек ли это», и расходится она ровно там, где список правят. Граф
    читает `service_accounts` из манифеста людей, витрина держала статику —
    значит claim такого бота перекрыл бы настоящего владельца.
    """
    manifest = tmp_path / "people.json"
    manifest.write_text('{"service_accounts": ["squad-bot"]}', encoding="utf-8")
    monkeypatch.setenv("PEOPLE_MANIFEST_PATH", str(manifest))
    monkeypatch.setattr(dash, "_SERVICE_ACCOUNTS", None)

    assert "squad-bot" in dash.service_accounts()
    assert "ai-agent" in dash.service_accounts(), "дефолт остаётся на месте"

    owner, source = dash.pick_owner(
        {"owner_login": "aoganisyan", "owner_source": "jira_assignee"},
        {"claim_owner": "squad-bot"},
    )
    assert (owner, source) == ("aoganisyan", "jira_assignee")


def test_missing_manifest_falls_back_to_defaults(dash, tmp_path, monkeypatch):
    """Манифест не смонтирован — витрина не падает и знает базовых ботов."""
    monkeypatch.setenv("PEOPLE_MANIFEST_PATH", str(tmp_path / "нет-такого.json"))
    monkeypatch.setattr(dash, "_SERVICE_ACCOUNTS", None)

    accounts = dash.service_accounts()
    assert accounts == dash.DEFAULT_SERVICE_ACCOUNTS


def test_broken_manifest_does_not_break_the_board(dash, tmp_path, monkeypatch):
    """Битый JSON — тоже не повод ронять витрину."""
    manifest = tmp_path / "people.json"
    manifest.write_text("{не json", encoding="utf-8")
    monkeypatch.setenv("PEOPLE_MANIFEST_PATH", str(manifest))
    monkeypatch.setattr(dash, "_SERVICE_ACCOUNTS", None)

    assert dash.service_accounts() == dash.DEFAULT_SERVICE_ACCOUNTS
