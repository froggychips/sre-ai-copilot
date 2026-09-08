"""Владелец namespace: один резолв вместо трёх карт.

08.09.2026: карта медика знала 15 логинов, из 19 текущих деплойеров сквадов
в ней не было 9 → 4 из 6 больных сквадов пинговались «владелец не
определён»; 5 сквадов были задеплоены сервисным аккаунтом ai-agent, человек
за ними виден только через ветку и Jira.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph import namespace_owner as no
from app.knowledge_graph.contract import (
    NAMESPACE_OWNER_SOURCE_DEPLOYED_BY, NAMESPACE_OWNER_SOURCE_JIRA_ASSIGNEE,
    NAMESPACE_OWNER_SOURCE_MANUAL, NAMESPACE_OWNER_SOURCE_TC_TRIGGERED_BY,
    NAMESPACE_OWNER_SOURCES)
from app.knowledge_graph.namespace_owner import (JiraUnavailable, PeopleManifest,
                                                 Person, TcUser,
                                                 jira_key_from_branch,
                                                 load_people_manifest,
                                                 resolve_owner,
                                                 sync_namespace_owners)
from app.knowledge_graph.schema import (NS_STATE_ACTIVE, NS_STATE_MISSING,
                                        Deployment, Namespace, Service)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _people(**kw) -> PeopleManifest:
    m = PeopleManifest()
    for login, extra in kw.items():
        m.people[login] = Person(login=login, **extra)
    return m


# --- ключ задачи из ветки ------------------------------------------------------


@pytest.mark.parametrize("branch,key", [
    ("wo-14169-tutorial-fake-truck", "WO-14169"),
    ("schabanov-wo-15194-greening-progress-double-count-preprod", "WO-15194"),
    ("WO-15450", "WO-15450"),
    ("refs-heads-preprod", None),
    ("preprod", None),
    ("squad-5", None),
    (None, None),
    ("wo-1", None),  # слишком короткий номер — не ключ
])
def test_jira_key_from_branch(branch, key):
    assert jira_key_from_branch(branch) == key


# --- порядок резолва -----------------------------------------------------------


def test_manual_override_wins():
    people = _people(alice={"discord_id": "1"})
    people.namespace_owners["squad-7-shared"] = "alice"
    res = resolve_owner("squad-7-shared", "bob", "wo-100-x", people=people, tc_users={},
                        jira_lookup=lambda k: {"account_id": "z", "email": None, "display_name": "Zed"})
    assert (res.login, res.source, res.discord_id) == ("alice", NAMESPACE_OWNER_SOURCE_MANUAL, "1")
    assert res.jira_key == "WO-100"


def test_jira_assignee_via_people_manifest_beats_deployed_by():
    """Тимлид раскатал чужую ветку: стенд того, чья задача."""
    people = _people(dev={"discord_id": "42", "jira_account_id": "acc-dev"})
    res = resolve_owner("squad-3-shared", "teamlead", "wo-14367-alliance", people=people, tc_users={},
                        jira_lookup=lambda k: {"account_id": "acc-dev", "email": None, "display_name": None})
    assert (res.login, res.source, res.discord_id) == ("dev", NAMESPACE_OWNER_SOURCE_JIRA_ASSIGNEE, "42")


def test_jira_assignee_matched_through_teamcity_profiles():
    """Без записи в манифесте логин находится по профилю TC: e-mail, затем имя."""
    tc = {"ddosta": TcUser("ddosta", name="Dmitry Dosta", email="dd@example.org"),
          "other": TcUser("other", name="Someone Else", email=None)}
    by_email = resolve_owner("squad-13-shared", "ai-agent", "wo-14516-afk", people=PeopleManifest(),
                             tc_users=tc, jira_lookup=lambda k: {"account_id": "x", "email": "DD@example.org",
                                                                 "display_name": "Другое Имя"})
    assert (by_email.login, by_email.source) == ("ddosta", NAMESPACE_OWNER_SOURCE_JIRA_ASSIGNEE)
    by_name = resolve_owner("squad-13-shared", "ai-agent", "wo-14516-afk", people=PeopleManifest(),
                            tc_users=tc, jira_lookup=lambda k: {"account_id": "x", "email": None,
                                                                "display_name": " dmitry   DOSTA "})
    assert by_name.login == "ddosta"
    assert by_name.discord_id is None, "Discord id только из манифеста"


def test_service_account_deployer_falls_through_to_tc_trigger():
    """squad-44: deployed-by=ai-agent, ветка preprod — человек только в истории TC."""
    res = resolve_owner("squad-44-shared", "ai-agent", "preprod", people=PeopleManifest(), tc_users={},
                        jira_lookup=None, triggered_by_fallback="wizaryx")
    assert (res.login, res.source) == ("wizaryx", NAMESPACE_OWNER_SOURCE_TC_TRIGGERED_BY)


def test_deployed_by_human_when_branch_has_no_task():
    people = _people(vdudnik={"discord_id": "7"})
    res = resolve_owner("squad-65-shared", "vdudnik", "preprod", people=people, tc_users={}, jira_lookup=None)
    assert (res.login, res.source, res.discord_id) == ("vdudnik", NAMESPACE_OWNER_SOURCE_DEPLOYED_BY, "7")


def test_jira_unavailable_degrades_to_next_path_and_is_flagged():
    def boom(key):
        raise JiraUnavailable("timeout")
    res = resolve_owner("squad-19-shared", "schabanov", "schabanov-wo-15194-x", people=PeopleManifest(),
                        tc_users={}, jira_lookup=boom)
    assert (res.login, res.source) == ("schabanov", NAMESPACE_OWNER_SOURCE_DEPLOYED_BY)
    assert res.jira_unavailable is True
    assert res.jira_key == "WO-15194"


def test_nothing_known_gives_unresolved_not_service_account():
    res = resolve_owner("squad-12-shared", "AI-Agent", "wo-15187-x", people=PeopleManifest(), tc_users={},
                        jira_lookup=lambda k: None, triggered_by_fallback="cicd")
    assert (res.login, res.source) == (None, None)
    assert res.jira_key == "WO-15187"


def test_every_source_value_is_in_contract():
    for src in (NAMESPACE_OWNER_SOURCE_MANUAL, NAMESPACE_OWNER_SOURCE_JIRA_ASSIGNEE,
                NAMESPACE_OWNER_SOURCE_DEPLOYED_BY, NAMESPACE_OWNER_SOURCE_TC_TRIGGERED_BY):
        assert src in NAMESPACE_OWNER_SOURCES


# --- манифест ---------------------------------------------------------------


def test_load_people_manifest(tmp_path):
    p = tmp_path / "people.json"
    p.write_text(json.dumps({
        "people": [{"login": "Alice", "discord_id": 123, "jira_email": "a@x.org"}, {"no_login": 1}],
        "service_accounts": ["Robo-Bot"],
        "namespace_owners": {"squad-1-shared": "ALICE"},
    }), encoding="utf-8")
    m = load_people_manifest(str(p))
    assert m.people["alice"].discord_id == "123"
    assert m.is_service_account("robo-bot") and m.is_service_account("ai-agent")
    assert m.namespace_owners == {"squad-1-shared": "alice"}
    assert m.discord_for("alice") == "123" and m.discord_for("nobody") is None


def test_missing_or_broken_manifest_is_empty_not_fatal(tmp_path):
    assert load_people_manifest("").people == {}
    assert load_people_manifest(str(tmp_path / "nope.json")).people == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_people_manifest(str(bad)).people == {}


# --- прогон по графу --------------------------------------------------------------


def test_sync_writes_owner_columns_for_scoped_active_namespaces(db, monkeypatch):
    monkeypatch.setattr(no.settings, "NAMESPACE_OWNER_SCOPE_REGEX", r"^squad-\d+-shared$")
    now = datetime(2026, 9, 8, 9, 9)
    db.add_all([
        Namespace(namespace="squad-64-shared", state=NS_STATE_ACTIVE,
                  deployed_by="vdudnik", deployed_branch="wo-14648-becky"),
        Namespace(namespace="squad-64-kingdom2", state=NS_STATE_ACTIVE, deployed_by="vdudnik"),
        Namespace(namespace="squad-42-shared", state=NS_STATE_MISSING, deployed_by="x"),
        Namespace(namespace="prod-shared", state=NS_STATE_ACTIVE, deployed_by="cicd"),
    ])
    db.commit()
    people = _people(vdudnik={"discord_id": "1182"})
    calls = []

    def jira(key):
        calls.append(key)
        return None  # задача без исполнителя → deployed_by

    stats = sync_namespace_owners(db, now=now, people=people, tc_users={}, jira_lookup=jira,
                                  activity_lookup=None)

    row = db.query(Namespace).filter_by(namespace="squad-64-shared").one()
    assert (row.owner_login, row.owner_source, row.owner_jira_key, row.owner_discord_id) == (
        "vdudnik", NAMESPACE_OWNER_SOURCE_DEPLOYED_BY, "WO-14648", "1182")
    assert row.owner_resolved_at == now
    for other in ("squad-64-kingdom2", "squad-42-shared", "prod-shared"):
        assert db.query(Namespace).filter_by(namespace=other).one().owner_login is None
    assert stats["scanned"] == 1 and stats["resolved"] == 1 and stats["changed"] == 1
    assert calls == ["WO-14648"]


def test_sync_uses_last_human_deploy_trigger_from_graph(db):
    ns = Namespace(namespace="squad-44-shared", state=NS_STATE_ACTIVE,
                   deployed_by="ai-agent", deployed_branch="preprod")
    svc = Service(name="town-service", namespace="squad-44-shared", synthetic=False)
    db.add_all([ns, svc])
    db.flush()
    db.add_all([
        Deployment(service_id=svc.id, triggered_by="ai-agent", started_at=datetime(2026, 9, 7, 10, 6)),
        Deployment(service_id=svc.id, triggered_by="wizaryx", started_at=datetime(2026, 9, 1, 8, 0)),
    ])
    db.commit()

    sync_namespace_owners(db, people=PeopleManifest(), tc_users={}, jira_lookup=None, activity_lookup=None)

    row = db.query(Namespace).filter_by(namespace="squad-44-shared").one()
    assert (row.owner_login, row.owner_source) == ("wizaryx", NAMESPACE_OWNER_SOURCE_TC_TRIGGERED_BY)


def test_sync_records_activity_and_jira_errors(db):
    db.add(Namespace(namespace="squad-19-shared", state=NS_STATE_ACTIVE,
                     deployed_by="ai-agent", deployed_branch="schabanov-wo-15194-x"))
    db.commit()

    def boom(key):
        raise JiraUnavailable("503")

    last = datetime(2026, 9, 8, 7, 0)
    stats = sync_namespace_owners(db, people=PeopleManifest(), tc_users={}, jira_lookup=boom,
                                  activity_lookup=lambda squad: last if squad == "squad-19" else None)

    row = db.query(Namespace).one()
    assert row.owner_login is None and row.owner_jira_key == "WO-15194"
    assert row.last_activity_at == last
    assert stats["jira_errors"] == 1 and stats["unresolved"] == 1 and stats["activity_updated"] == 1
