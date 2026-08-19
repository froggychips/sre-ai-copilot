"""PR из чужого форка не должен выполняться на self-hosted раннере.

Security finding 19.08.2026. Раннер здесь persistent и работает под
пользователем разработчика: рядом с ним лежат `~/.kube/config` с доступом к
прод-кластеру, `~/.ssh/id_rsa`, `~/.aws/credentials`, `~/.docker/config.json`
и токены. Выполнить на нём код из чужого PR — значит отдать всё это автору PR.

Репозиторий публичный, форки разрешены, GitHub-hosted раннеры недоступны
(аккаунт отрезан от них по биллингу) — то есть «просто перенести PR-проверки
в облако» здесь не вариант.

Две защиты, и обе нужны:

  * **процедурная** — `approval_policy: all_external_contributors` в
    настройках репозитория. Держится на внимательности человека, нажимающего
    кнопку. Дефолт был `first_time_contributors`, что давало двухходовку:
    безобидная правка → одобрение → дальше запуск без подтверждения;
  * **структурная** — условие в самом workflow. Job с форка не стартует
    вовсе, независимо от того, что кто-то одобрил.

Этот тест сторожит вторую: настройки репозитория из кода не видны, а
workflow — видны.
"""
import pathlib

import pytest
import yaml

WORKFLOWS = pathlib.Path(__file__).parent.parent / ".github" / "workflows"


def _jobs(path: pathlib.Path) -> dict:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("jobs", {})


def _triggers_on_pull_request(path: pathlib.Path) -> bool:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    # `on` в YAML разбирается как булево True — известная особенность.
    on = data.get("on", data.get(True, {})) or {}
    return "pull_request" in (on if isinstance(on, dict) else {on: None})


def _uses_self_hosted(job: dict) -> bool:
    runs_on = job.get("runs-on", "")
    if isinstance(runs_on, list):
        return "self-hosted" in runs_on
    return "self-hosted" in str(runs_on)


def _pr_workflow_jobs():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        if not _triggers_on_pull_request(path):
            continue
        for name, job in _jobs(path).items():
            if _uses_self_hosted(job):
                yield path.name, name, job


def test_there_are_pr_triggered_self_hosted_jobs():
    """Сам факт: такие job'ы есть, и именно поэтому нужна защита."""
    assert list(_pr_workflow_jobs()), (
        "не нашлось ни одного PR-job на self-hosted — проверь парсинг, "
        "а не радуйся"
    )


@pytest.mark.parametrize("workflow,job_name,job", list(_pr_workflow_jobs()))
def test_pr_job_refuses_foreign_forks(workflow, job_name, job):
    """У каждого такого job должно быть условие про происхождение PR."""
    condition = str(job.get("if", ""))
    assert condition, (
        f"{workflow}:{job_name} стартует на self-hosted по PR без всяких "
        "условий — форк выполнит свой код на машине с доступом к проду"
    )
    mentions_fork = ("head.repo.full_name" in condition
                     or "github.repository" in condition)
    # Второй законный вариант: job вообще не запускается на PR (например,
    # сборка образа идёт только на push). Тогда чужой форк до раннера не
    # доходит по определению.
    excludes_pr = "github.event_name == 'push'" in condition.replace('"', "'")
    assert mentions_fork or excludes_pr, (
        f"{workflow}:{job_name} — условие есть, но оно не защищает от форка "
        f"и не исключает PR: {condition!r}"
    )


def test_condition_still_allows_own_branches():
    """Защита не должна ломать обычную работу: push и свои ветки идут."""
    for workflow, job_name, job in _pr_workflow_jobs():
        condition = str(job.get("if", ""))
        assert "github.event_name" in condition, (
            f"{workflow}:{job_name} — условие не упоминает тип события, "
            "значит поведение на push непредсказуемо"
        )
