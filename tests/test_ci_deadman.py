"""Тесты CI-канарейки (scripts/ci_deadman.py).

Канарейка сама себя не проверяет: её единственный потребитель — Discord, и
молчащая канарейка неотличима от здорового CI. Поэтому логику находок держим
под тестами, особенно два неочевидных свойства:

  * очередь важнее статуса раннера — умерший процесс раннера какое-то время
    ещё числится online, а прогоны уже копятся в queued;
  * упавшая проверка САМА становится находкой, а не тихо пропускается.
"""
import datetime
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "ci_deadman", REPO_ROOT / "scripts" / "ci_deadman.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def dm():
    return _load()


def _iso(minutes_ago=0, days_ago=0):
    ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        minutes=minutes_ago, days=days_ago
    )
    return ts.isoformat().replace("+00:00", "Z")


# --- раннеры --------------------------------------------------------------


def test_offline_runner_is_a_finding(dm, monkeypatch):
    monkeypatch.setattr(dm, "gh", lambda *a, **k: {
        "runners": [{"name": "jabbook-air-m3-sre-copilot", "status": "offline"}]
    })
    problems = dm.check_runners()
    assert len(problems) == 1
    assert "offline" in problems[0]


def test_no_runners_at_all_is_a_finding(dm, monkeypatch):
    monkeypatch.setattr(dm, "gh", lambda *a, **k: {"runners": []})
    assert dm.check_runners(), "пустой список раннеров обязан быть находкой"


def test_online_runner_is_silent(dm, monkeypatch):
    monkeypatch.setattr(dm, "gh", lambda *a, **k: {
        "runners": [{"name": "jabbook-air-m3-sre-copilot", "status": "online"}]
    })
    assert dm.check_runners() == []


def test_watched_runner_missing_is_a_finding(dm, monkeypatch):
    """Раннер переименовали/снесли — молчать нельзя: гонять больше не на чем."""
    monkeypatch.setattr(dm, "RUNNER_NAME", "jabbook-air-m3-sre-copilot")
    monkeypatch.setattr(dm, "gh", lambda *a, **k: {
        "runners": [{"name": "someone-elses-runner", "status": "online"}]
    })
    problems = dm.check_runners()
    assert problems and "не зарегистрирован" in problems[0]


# --- очередь --------------------------------------------------------------


def test_stuck_queue_is_a_finding(dm, monkeypatch):
    monkeypatch.setattr(dm, "QUEUE_MINUTES", 30)
    monkeypatch.setattr(dm, "gh", lambda *a, **k: {
        "workflow_runs": [
            {"name": "CI Pipeline", "run_number": 7, "html_url": "u", "created_at": _iso(minutes_ago=90)}
        ]
    })
    problems = dm.check_queue()
    assert problems and "очередь стоит" in problems[0]


def test_fresh_queue_is_silent(dm, monkeypatch):
    """Прогон, стоящий пару минут, — норма: раннер один, PR-ы ждут очереди."""
    monkeypatch.setattr(dm, "QUEUE_MINUTES", 30)
    monkeypatch.setattr(dm, "gh", lambda *a, **k: {
        "workflow_runs": [
            {"name": "CI Pipeline", "run_number": 8, "html_url": "u", "created_at": _iso(minutes_ago=3)}
        ]
    })
    assert dm.check_queue() == []


# --- зависшие зелёные PR --------------------------------------------------


def _pr(number, days, sha="abc"):
    return {
        "number": number,
        "title": f"chore(deps): bump thing {number}",
        "html_url": f"https://example/pull/{number}",
        "created_at": _iso(days_ago=days),
        "head": {"sha": sha},
        "draft": False,
    }


def test_old_green_pr_is_a_finding(dm, monkeypatch):
    monkeypatch.setattr(dm, "STALE_PR_DAYS", 3)

    def fake_gh(path, params=None):
        if path.endswith("/pulls"):
            return [_pr(250, days=5)]
        return {"check_runs": [{"status": "completed", "conclusion": "success"}]}

    monkeypatch.setattr(dm, "gh", fake_gh)
    problems = dm.check_stale_green_prs()
    assert problems and "#250" in problems[0]


def test_red_pr_is_not_a_stale_finding(dm, monkeypatch):
    """Красный PR — это не дедлок, а работа автора; канарейка о нём молчит."""
    monkeypatch.setattr(dm, "STALE_PR_DAYS", 3)

    def fake_gh(path, params=None):
        if path.endswith("/pulls"):
            return [_pr(251, days=5)]
        return {"check_runs": [{"status": "completed", "conclusion": "failure"}]}

    monkeypatch.setattr(dm, "gh", fake_gh)
    assert dm.check_stale_green_prs() == []


def test_pending_checks_pr_is_not_a_stale_finding(dm, monkeypatch):
    """Проверки ещё идут — вердикта нет, находки тоже."""
    monkeypatch.setattr(dm, "STALE_PR_DAYS", 3)

    def fake_gh(path, params=None):
        if path.endswith("/pulls"):
            return [_pr(252, days=5)]
        return {"check_runs": [{"status": "in_progress", "conclusion": None}]}

    monkeypatch.setattr(dm, "gh", fake_gh)
    assert dm.check_stale_green_prs() == []


def test_young_pr_is_silent(dm, monkeypatch):
    monkeypatch.setattr(dm, "STALE_PR_DAYS", 3)

    def fake_gh(path, params=None):
        if path.endswith("/pulls"):
            return [_pr(253, days=1)]
        raise AssertionError("до проверок молодого PR доходить не должны")

    monkeypatch.setattr(dm, "gh", fake_gh)
    assert dm.check_stale_green_prs() == []


# --- красная дефолтная ветка ---------------------------------------------


def test_red_default_branch_is_a_finding(dm, monkeypatch):
    monkeypatch.setattr(dm, "DEFAULT_BRANCH", "master")
    monkeypatch.setattr(dm, "gh", lambda *a, **k: {
        "workflow_runs": [
            {"name": "CI Pipeline", "run_number": 9, "conclusion": "failure",
             "html_url": "u", "updated_at": _iso(minutes_ago=10)}
        ]
    })
    problems = dm.check_default_branch_red()
    assert problems and "красный" in problems[0]


def test_codeql_failure_does_not_count_as_red_ci(dm, monkeypatch):
    """Ветку красит только CI Pipeline: у CodeQL своя история и свои причины."""
    monkeypatch.setattr(dm, "DEFAULT_BRANCH", "master")
    monkeypatch.setattr(dm, "gh", lambda *a, **k: {
        "workflow_runs": [
            {"name": "CodeQL", "run_number": 9, "conclusion": "failure",
             "html_url": "u", "updated_at": _iso(minutes_ago=10)}
        ]
    })
    assert dm.check_default_branch_red() == []


# --- устойчивость самой канарейки ----------------------------------------


def test_failing_check_becomes_a_finding_and_others_still_run(dm, monkeypatch):
    """Сеть/токен отвалились — это находка, а не повод молча вернуть «всё ок»."""
    calls = []

    def boom():
        raise RuntimeError("401 Bad credentials")

    def ok():
        calls.append("ok")
        return []

    monkeypatch.setattr(dm, "CHECKS", (("runners", boom), ("queue", ok)))
    problems = dm.run_checks()
    assert calls == ["ok"], "падение одной проверки не должно отменять остальные"
    assert len(problems) == 1
    assert "не отработала" in problems[0] and "RuntimeError" in problems[0]


def test_clean_run_posts_nothing(dm, monkeypatch):
    """Молчание при здоровом CI — обязательное свойство, иначе канал глушат."""
    monkeypatch.setattr(dm, "TOKEN", "t")
    monkeypatch.setattr(dm, "CHECKS", (("runners", lambda: []),))
    posted = []
    monkeypatch.setattr(dm, "post_discord", lambda p: posted.append(p))
    assert dm.main() == 0
    assert posted == []


def test_findings_are_posted(dm, monkeypatch):
    monkeypatch.setattr(dm, "TOKEN", "t")
    monkeypatch.setattr(dm, "CHECKS", (("runners", lambda: ["раннер offline"]),))
    posted = []
    monkeypatch.setattr(dm, "post_discord", lambda p: posted.append(p))
    assert dm.main() == 1
    assert posted == [["раннер offline"]]


def test_no_token_exits_nonzero(dm, monkeypatch):
    """Без токена канарейка обязана падать заметно, а не «проверять» вхолостую."""
    monkeypatch.setattr(dm, "TOKEN", "")
    assert dm.main() == 2
