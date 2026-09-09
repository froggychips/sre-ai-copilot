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
    NAMESPACE_OWNER_SOURCE_DEPLOYED_BY, NAMESPACE_OWNER_SOURCE_GD_CLAIM,
    NAMESPACE_OWNER_SOURCE_JIRA_ASSIGNEE, NAMESPACE_OWNER_SOURCE_MANUAL,
    NAMESPACE_OWNER_SOURCE_TC_TRIGGERED_BY, NAMESPACE_OWNER_SOURCES)
from app.knowledge_graph.namespace_owner import (JiraUnavailable, PeopleManifest,
                                                 Person, TcUser,
                                                 gd_claims_from_builds,
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


# --- кнопка «занять / освободить» (gd_claim) ---------------------------------


def _gd_build(squad, login, *, status="SUCCESS", state="finished"):
    """Ответ TeamCity по кнопке: номер стенда только в resulting-properties."""
    return {
        "status": status, "state": state,
        "triggered": {"user": {"username": login}},
        "resultingProperties": {"property": [
            {"name": "TARGET_SQUAD", "value": ""},          # авто-выбор: пусто
            {"name": "RESOLVED_SQUAD", "value": squad},
        ]},
    }


def test_gd_claim_beats_stale_deployed_by_label():
    """09.09.2026: стенд занят кнопкой, а лейбл остался от прежнего деплойера."""
    res = resolve_owner("squad-8-shared", "grozoff", "default", people=PeopleManifest(),
                        tc_users={}, jira_lookup=None, gd_claim_login="akomkov")
    assert (res.login, res.source) == ("akomkov", NAMESPACE_OWNER_SOURCE_GD_CLAIM)


def test_jira_assignee_still_beats_gd_claim():
    """1.0.13: «владелец из задачи, а не из кнопки» — приоритет не меняем."""
    people = _people(ddosta={"jira_account_id": "acc-1"})
    res = resolve_owner("squad-13-shared", "grozoff", "wo-14516-alliance-afk-leader",
                        people=people, tc_users={},
                        jira_lookup=lambda k: {"account_id": "acc-1", "email": None,
                                               "display_name": None},
                        gd_claim_login="akomkov")
    assert (res.login, res.source) == ("ddosta", NAMESPACE_OWNER_SOURCE_JIRA_ASSIGNEE)


def test_gd_claim_by_service_account_is_ignored():
    """Кнопку дёрнули REST-ом сервисным токеном — владельцем агент не становится."""
    res = resolve_owner("squad-44-shared", "vdudnik", "preprod", people=PeopleManifest(),
                        tc_users={}, jira_lookup=None, gd_claim_login="ai-agent")
    assert (res.login, res.source) == ("vdudnik", NAMESPACE_OWNER_SOURCE_DEPLOYED_BY)


def test_gd_claims_parser_takes_latest_success_per_squad():
    builds = [
        _gd_build("squad-15", "akomkov"),                      # свежее — оно и победит
        _gd_build("squad-15", "grozoff"),
        _gd_build("squad-8", "akomkov"),
        _gd_build("squad-20", "abugar", status="FAILURE"),      # неудачная попытка
        _gd_build("squad-21", "someone", state="running"),      # ещё идёт
        {"status": "SUCCESS", "state": "finished",              # освобождение без номера
         "triggered": {"user": {"username": "x"}},
         "resultingProperties": {"property": [{"name": "RESOLVED_SQUAD", "value": ""}]}},
    ]
    assert gd_claims_from_builds(builds) == {"squad-15": "akomkov", "squad-8": "akomkov"}


def test_sync_prefers_gd_claim_over_label(db):
    """Сквозь прогон: лейбл чужой, кнопку нажал другой человек → владелец из кнопки."""
    db.add(Namespace(namespace="squad-8-shared", state=NS_STATE_ACTIVE,
                     deployed_by="grozoff", deployed_branch="default"))
    db.commit()
    stats = sync_namespace_owners(db, people=PeopleManifest(), tc_users={}, jira_lookup=None,
                                  activity_lookup=None, gd_claims={"squad-8": "akomkov"})
    row = db.query(Namespace).filter_by(namespace="squad-8-shared").one()
    assert (row.owner_login, row.owner_source) == ("akomkov", NAMESPACE_OWNER_SOURCE_GD_CLAIM)
    assert stats["gd_claims"] == 1


def test_every_source_value_is_in_contract():
    for src in (NAMESPACE_OWNER_SOURCE_MANUAL, NAMESPACE_OWNER_SOURCE_JIRA_ASSIGNEE,
                NAMESPACE_OWNER_SOURCE_GD_CLAIM, NAMESPACE_OWNER_SOURCE_DEPLOYED_BY,
                NAMESPACE_OWNER_SOURCE_TC_TRIGGERED_BY):
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


# --- токен для профилей TeamCity ---------------------------------------------
#
# 08.09.2026 на проде: TC_TOKEN принадлежит сервисной учётке `ai-agent`, у неё
# нет права смотреть профили → 403, и путь `jira_assignee` молча деградировал
# до `deployed_by` на всех 46 стендах. Отдельный TC_USERS_TOKEN это лечит.


class _FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise no.httpx.HTTPStatusError("boom", request=None, response=self)


class _FakeClient:
    """Пишет использованный токен в `seen`, отдаёт заготовленный ответ."""

    def __init__(self, response, seen: dict):
        self._response = response
        self._seen = seen

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None, params=None):
        self._seen["url"] = url
        self._seen["auth"] = (headers or {}).get("Authorization")
        return self._response


def _install_client(monkeypatch, response):
    seen: dict = {}
    monkeypatch.setattr(no.httpx, "Client", lambda **kw: _FakeClient(response, seen))
    return seen


def test_dedicated_users_token_wins_over_main_token(monkeypatch):
    monkeypatch.setattr(no.settings, "TC_URL", "https://tc.example.org")
    monkeypatch.setattr(no.settings, "TC_TOKEN", "service-account-token")
    monkeypatch.setattr(no.settings, "TC_USERS_TOKEN", "profiles-token")
    payload = {"user": [{"username": "Ddosta", "name": "Dmitry Dosta", "email": "dd@example.org"}]}
    seen = _install_client(monkeypatch, _FakeResponse(200, payload))

    users = no.fetch_tc_users()

    assert seen["auth"] == "Bearer profiles-token"
    assert users["ddosta"].email == "dd@example.org"


def test_falls_back_to_main_token_when_dedicated_is_empty(monkeypatch):
    monkeypatch.setattr(no.settings, "TC_URL", "https://tc.example.org")
    monkeypatch.setattr(no.settings, "TC_TOKEN", "service-account-token")
    monkeypatch.setattr(no.settings, "TC_USERS_TOKEN", "")
    seen = _install_client(monkeypatch, _FakeResponse(200, {"user": []}))

    assert no.fetch_tc_users() == {}
    assert seen["auth"] == "Bearer service-account-token"


def test_forbidden_is_not_fatal_and_names_the_cause(monkeypatch, caplog):
    monkeypatch.setattr(no.settings, "TC_URL", "https://tc.example.org")
    monkeypatch.setattr(no.settings, "TC_TOKEN", "service-account-token")
    monkeypatch.setattr(no.settings, "TC_USERS_TOKEN", "")
    _install_client(monkeypatch, _FakeResponse(403))

    assert no.fetch_tc_users() == {}, "403 — пустой справочник, а не исключение"


def test_no_token_at_all_skips_the_call(monkeypatch):
    monkeypatch.setattr(no.settings, "TC_URL", "https://tc.example.org")
    monkeypatch.setattr(no.settings, "TC_TOKEN", "")
    monkeypatch.setattr(no.settings, "TC_USERS_TOKEN", "")

    def _boom(**kw):
        raise AssertionError("без токена запрос делать нельзя")

    monkeypatch.setattr(no.httpx, "Client", _boom)
    assert no.fetch_tc_users() == {}


def test_sync_commits_in_batches_not_one_long_transaction(db, monkeypatch):
    """Одна транзакция на весь прогон мешает соседям: 08.09.2026 миграция не
    взяла лок за lock_timeout=15s из-за чужой транзакции возрастом 98 с, а этот
    прогон длится ~95 с (внутри — запросы в Jira)."""
    monkeypatch.setattr(no.settings, "NAMESPACE_OWNER_SCOPE_REGEX", r"^squad-\d+-shared$")
    monkeypatch.setattr(no, "_COMMIT_EVERY", 3)
    for i in range(1, 8):
        db.add(Namespace(namespace=f"squad-{i}-shared", state=NS_STATE_ACTIVE,
                         deployed_by=f"dev{i}", deployed_branch="preprod"))
    db.commit()

    commits: list[int] = []
    real_commit = db.commit
    scanned = {"n": 0}

    def counting_commit():
        commits.append(scanned["n"])
        real_commit()

    monkeypatch.setattr(db, "commit", counting_commit)
    orig_resolve = no.resolve_owner

    def counting_resolve(*a, **kw):
        scanned["n"] += 1
        return orig_resolve(*a, **kw)

    monkeypatch.setattr(no, "resolve_owner", counting_resolve)

    stats = no.sync_namespace_owners(db, people=PeopleManifest(), tc_users={},
                                     jira_lookup=None, activity_lookup=None)

    assert stats["resolved"] == 7
    assert len(commits) >= 3, f"ожидались промежуточные коммиты, было: {commits}"
    assert commits[0] <= 3, "первый коммит должен случиться до конца прогона"
