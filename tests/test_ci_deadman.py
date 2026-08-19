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
def dm(monkeypatch):
    """Модуль канарейки, отвязанный от окружения запуска.

    GitHub Actions экспортирует в job переменные RUNNER_NAME и GITHUB_TOKEN, а
    модуль читает их на импорте. Из-за этого тесты вели себя по-разному
    локально и в CI: на раннере `jabbook-air-m3-sre-copilot` проверка уходила
    в ветку «нужный раннер не зарегистрирован» и роняла сборку, зелёную на
    ноутбуке. Значения задаём явно — тест не должен зависеть от того, где его
    запустили.
    """
    mod = _load()
    monkeypatch.setattr(mod, "RUNNER_NAME", "")   # пусто → смотрим все раннеры
    monkeypatch.setattr(mod, "TOKEN", "")         # анонимный режим по умолчанию
    return mod


def _iso(minutes_ago=0, days_ago=0):
    ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        minutes=minutes_ago, days=days_ago
    )
    return ts.isoformat().replace("+00:00", "Z")


# --- раннеры --------------------------------------------------------------
#
# Токен здесь обязателен: без него проверка штатно пропускается (см. секцию
# «работа без токена»), и тест на молчание при живом раннере зеленел бы по
# ложной причине — пустой список от пропуска неотличим от «всё хорошо».


@pytest.fixture
def token(dm, monkeypatch):
    monkeypatch.setattr(dm, "TOKEN", "t")
    return dm


def test_offline_runner_is_a_finding(token, monkeypatch):
    monkeypatch.setattr(token, "gh", lambda *a, **k: {
        "runners": [{"name": "jabbook-air-m3-sre-copilot", "status": "offline"}]
    })
    problems = token.check_runners()
    assert len(problems) == 1
    assert "offline" in problems[0]


def test_no_runners_at_all_is_a_finding(token, monkeypatch):
    monkeypatch.setattr(token, "gh", lambda *a, **k: {"runners": []})
    assert token.check_runners(), "пустой список раннеров обязан быть находкой"


def test_online_runner_is_silent(token, monkeypatch):
    monkeypatch.setattr(token, "gh", lambda *a, **k: {
        "runners": [{"name": "jabbook-air-m3-sre-copilot", "status": "online"}]
    })
    assert token.check_runners() == []


def test_watched_runner_missing_is_a_finding(token, monkeypatch):
    """Раннер переименовали/снесли — молчать нельзя: гонять больше не на чем."""
    monkeypatch.setattr(token, "RUNNER_NAME", "jabbook-air-m3-sre-copilot")
    monkeypatch.setattr(token, "gh", lambda *a, **k: {
        "runners": [{"name": "someone-elses-runner", "status": "online"}]
    })
    problems = token.check_runners()
    assert problems and "не зарегистрирован" in problems[0]


# --- очередь --------------------------------------------------------------


def test_stuck_queue_is_a_finding(dm, monkeypatch):
    """Очередь ждёт, и НИКТО ничего не выполняет — раннер не разбирает её.

    Второй запрос (in_progress) отдаёт пусто намеренно: именно отсутствие
    работы отличает вставшую очередь от занятого раннера.
    """
    monkeypatch.setattr(dm, "QUEUE_MINUTES", 30)
    monkeypatch.setattr(dm, "STUCK_HOURS", 2)   # чтобы не попасть в hard-порог

    def fake_gh(path, params=None):
        if (params or {}).get("status") == "queued":
            return {"workflow_runs": [{
                "name": "CI Pipeline", "run_number": 7, "html_url": "u",
                "created_at": _iso(minutes_ago=90),
            }]}
        return {"workflow_runs": []}

    monkeypatch.setattr(dm, "gh", fake_gh)
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


# --- работа без токена ----------------------------------------------------
#
# Репозиторий публичный: очередь, PR-ы и ветка читаются анонимно, а 401 отдаёт
# только `/actions/runners`. Класть в кластер полноправный токен ради одной
# проверки — размен не в нашу пользу, поэтому режим без токена штатный.


def test_anonymous_run_works(dm, monkeypatch):
    """Нет токена — канарейка работает, а не выходит с ошибкой."""
    monkeypatch.setattr(dm, "TOKEN", "")
    monkeypatch.setattr(dm, "CHECKS", (("queue", lambda: []),))
    monkeypatch.setattr(dm, "post_discord", lambda p: None)
    assert dm.main() == 0


def test_runners_check_is_skipped_without_token(dm, monkeypatch):
    """Пропуск тихий: ежечасная жалоба на известное ограничение = шум."""
    monkeypatch.setattr(dm, "TOKEN", "")
    monkeypatch.setattr(dm, "gh", lambda *a, **k: pytest.fail("не должно ходить в API"))
    assert dm.check_runners() == []


def test_runners_check_runs_when_token_present(dm, monkeypatch):
    """Появился токен — проверка включается сама, без правок конфигурации."""
    monkeypatch.setattr(dm, "TOKEN", "t")
    monkeypatch.setattr(dm, "gh", lambda *a, **k: {
        "runners": [{"name": "mac", "status": "offline"}]
    })
    problems = dm.check_runners()
    assert problems and "offline" in problems[0]


def test_no_auth_header_without_token(dm, monkeypatch):
    captured = {}

    class R:
        status_code = 200
        headers: dict = {}

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured.update(headers or {})
        return R()

    monkeypatch.setattr(dm, "TOKEN", "")
    monkeypatch.setattr(dm.requests, "get", fake_get)
    dm.gh("/x")
    assert "Authorization" not in captured


def test_rate_limit_is_distinct_from_broken_ci(dm, monkeypatch):
    """Лимит 60/час на IP — это «мы ослепли», и молчать об этом нельзя."""
    class R:
        status_code = 403
        headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Limit": "60",
                   "X-RateLimit-Reset": "1"}

        @staticmethod
        def raise_for_status():
            raise AssertionError("должно упасть раньше, своим типом")

    monkeypatch.setattr(dm, "TOKEN", "")
    monkeypatch.setattr(dm.requests, "get", lambda *a, **k: R())
    with pytest.raises(dm.RateLimited):
        dm.gh("/x")


def test_forbidden_without_exhausted_limit_is_not_rate_limit(dm, monkeypatch):
    """403 с остатком запросов — это отказ в доступе, другая болезнь."""
    class R:
        status_code = 403
        headers = {"X-RateLimit-Remaining": "42"}

        @staticmethod
        def raise_for_status():
            raise RuntimeError("403 Forbidden")

    monkeypatch.setattr(dm, "TOKEN", "")
    monkeypatch.setattr(dm.requests, "get", lambda *a, **k: R())
    with pytest.raises(RuntimeError) as e:
        dm.gh("/x")
    assert not isinstance(e.value, dm.RateLimited)


# --- отменённый прогон как след офлайн-раннера ----------------------------


def test_cancelled_run_on_default_branch_is_a_finding(dm, monkeypatch):
    """Так выглядит офлайн-раннер снаружи: очередь стоит, GitHub отменяет.

    Заменяет прямую проверку раннера в анонимном режиме — master руками
    никто не отменяет.
    """
    monkeypatch.setattr(dm, "DEFAULT_BRANCH", "master")
    monkeypatch.setattr(dm, "gh", lambda *a, **k: {
        "workflow_runs": [
            {"name": "CI Pipeline", "run_number": 9, "conclusion": "cancelled",
             "html_url": "u", "updated_at": _iso(minutes_ago=10)}
        ]
    })
    problems = dm.check_default_branch_red()
    assert problems and "отменён" in problems[0]


def test_successful_run_stays_silent(dm, monkeypatch):
    monkeypatch.setattr(dm, "DEFAULT_BRANCH", "master")
    monkeypatch.setattr(dm, "gh", lambda *a, **k: {
        "workflow_runs": [
            {"name": "CI Pipeline", "run_number": 9, "conclusion": "success",
             "html_url": "u", "updated_at": _iso(minutes_ago=10)}
        ]
    })
    assert dm.check_default_branch_red() == []


# --- срез по лимиту не выдаётся за полную проверку ------------------------


def test_pr_cap_is_reported_not_silent(dm, monkeypatch):
    """Проверено не всё — канарейка обязана сказать это вслух."""
    monkeypatch.setattr(dm, "MAX_PR_CHECKS", 1)
    prs = [
        {"number": n, "title": f"pr{n}", "draft": False,
         "created_at": _iso(days_ago=10), "html_url": "u",
         "head": {"sha": f"sha{n}"}}
        for n in (1, 2, 3)
    ]

    def fake_gh(path, params=None):
        if path.endswith("/pulls"):
            return prs
        return {"check_runs": [{"status": "completed", "conclusion": "success"}]}

    monkeypatch.setattr(dm, "gh", fake_gh)
    problems = dm.check_stale_green_prs()
    assert any("2 пропущено" in p for p in problems)


def test_only_runners_check_requires_a_token(dm):
    """Флаг живёт на функции — если его потеряют, пропуск станет ложным «OK»."""
    assert dm.check_runners.needs_token is True
    others = [fn for name, fn in dm.CHECKS if name != "runners"]
    assert not any(getattr(fn, "needs_token", False) for fn in others), (
        "анонимный режим сломается: проверка требует токен, но не помечена"
    )


# --- очередь: «есть задачи» ≠ «стоит» -------------------------------------
#
# 17.08.2026 канарейка дважды подряд сообщила «очередь стоит» на трёх
# прогонах, ждавших 36-38 минут, — при живом раннере, который в этот момент
# честно собирал образ. Раннер здесь ОДИН, и такая очередь нормальна.
#
# Ложные срабатывания приучают игнорировать канал, то есть ломают канарейку
# вернее, чем её отсутствие.


def _queued(dm, minutes_ago, n=1):
    return {"workflow_runs": [
        {"name": "CI Pipeline", "run_number": 800 + i, "html_url": "u",
         "created_at": _iso(minutes_ago=minutes_ago)}
        for i in range(n)
    ]}


def test_long_queue_is_silent_while_something_runs(dm, monkeypatch):
    """Раннер занят — очередь движется, это не авария."""
    monkeypatch.setattr(dm, "QUEUE_MINUTES", 30)

    def fake_gh(path, params=None):
        if (params or {}).get("status") == "queued":
            return _queued(dm, 38, n=3)
        return {"workflow_runs": [{"name": "CI Pipeline", "run_number": 855}]}

    monkeypatch.setattr(dm, "gh", fake_gh)
    assert dm.check_queue() == [], "очередь при работающем раннере — норма"


def test_queue_with_nothing_running_is_a_finding(dm, monkeypatch):
    """Никто не выполняется, а прогоны ждут — вот это встало."""
    monkeypatch.setattr(dm, "QUEUE_MINUTES", 30)

    def fake_gh(path, params=None):
        if (params or {}).get("status") == "queued":
            return _queued(dm, 38, n=2)
        return {"workflow_runs": []}

    monkeypatch.setattr(dm, "gh", fake_gh)
    problems = dm.check_queue()
    assert problems and "очередь стоит" in problems[0]
    assert "ничего не выполняется" in problems[0]


def test_hours_long_wait_is_a_finding_even_with_a_busy_runner(dm, monkeypatch):
    """Часы ожидания при занятом раннере — он занят чем-то, что не кончается."""
    monkeypatch.setattr(dm, "STUCK_HOURS", 2)

    def fake_gh(path, params=None):
        if (params or {}).get("status") == "queued":
            return _queued(dm, 200)          # 3 с лишним часа
        return {"workflow_runs": [{"name": "CI Pipeline", "run_number": 855}]}

    monkeypatch.setattr(dm, "gh", fake_gh)
    problems = dm.check_queue()
    assert problems and "висят дольше" in problems[0]


def test_empty_queue_skips_the_second_request(dm, monkeypatch):
    """Нет очереди — не тратим лимит на запрос про выполняющиеся."""
    calls = []

    def fake_gh(path, params=None):
        calls.append((params or {}).get("status"))
        return {"workflow_runs": []}

    monkeypatch.setattr(dm, "gh", fake_gh)
    assert dm.check_queue() == []
    assert calls == ["queued"], "лишний запрос при пустой очереди"


def test_fresh_queue_stays_silent_even_with_idle_runner(dm, monkeypatch):
    """Прогон, ждущий пару минут, — норма при любом состоянии раннера."""
    monkeypatch.setattr(dm, "QUEUE_MINUTES", 30)

    def fake_gh(path, params=None):
        if (params or {}).get("status") == "queued":
            return _queued(dm, 3)
        return {"workflow_runs": []}

    monkeypatch.setattr(dm, "gh", fake_gh)
    assert dm.check_queue() == []


def test_hard_threshold_matches_a_real_ci_run_length(dm):
    """Порог «висит слишком долго» должен быть кратен обычному прогону.

    Инцидент 19.08.2026: pytest завис в `wait_for_thread_shutdown` (фоновый
    поток держал сокет к kube-apiserver), раннер полтора часа числился busy,
    очередь стояла — а канарейка молчала, потому что порог был 2 часа.

    Полный прогон CI здесь занимает ~11 минут. Порог обязан оставлять запас
    на очередь, но не превращаться в «полдня простоя — это нормально».
    """
    typical_run_minutes = 11
    assert dm.STUCK_HOURS * 60 >= typical_run_minutes * 3, (
        "порог меньше трёх обычных прогонов — будут ложные срабатывания "
        "на честной очереди"
    )
    assert dm.STUCK_HOURS <= 1.0, (
        "порог больше часа: столько CI уже не должен молчать при одном раннере"
    )
