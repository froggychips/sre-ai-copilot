"""PR из чужого форка не должен выполняться на нашем раннере.

Security finding 19.08.2026. Тогда раннер был persistent и работал под
пользователем разработчика: рядом лежали `~/.kube/config` с доступом к
прод-кластеру, `~/.ssh/id_rsa`, `~/.aws/credentials` и токены. Выполнить на
нём код из чужого PR — значит отдать всё это автору PR.

С переездом на ARC (17.09.2026) ключей разработчика рядом больше нет: под
эфемерный, в отдельном namespace, без доступа к kube-apiserver и без токена
ServiceAccount. Требование осталось: чужой код — всё ещё чужой код, а
dind-сайдкар privileged.

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


#: Префиксы образов GitHub-hosted. Всё остальное исполняется на НАШЕЙ
#: инфраструктуре, даже когда слова "self-hosted" в runs-on нет.
_GITHUB_HOSTED_PREFIXES = ("ubuntu-", "windows-", "macos-")


def _uses_self_hosted(job: dict) -> bool:
    """Job исполняется на нашей инфраструктуре, а не на GitHub-hosted.

    Проверка по слову "self-hosted" перестала работать после переезда на
    ARC 17.09.2026: у runner scale set в `runs-on` пишется ИМЯ НАБОРА
    (`sre-copilot-k8s`), лейбла self-hosted там нет вовсе. Тест ниже честно
    поймал это падением — он и задуман так, чтобы молчаливое «ничего не
    нашлось» считалось поломкой парсинга, а не поводом радоваться.

    Поэтому признак инвертирован: self-hosted = НЕ GitHub-hosted образ.
    Так новый набор раннеров попадает под проверку изоляции сам, без
    правки списка при каждом переименовании.
    """
    runs_on = job.get("runs-on", "")
    labels = runs_on if isinstance(runs_on, list) else [runs_on]
    labels = [str(label) for label in labels if label]
    if not labels:
        return False
    if any("self-hosted" in label for label in labels):
        return True
    return not any(
        label.startswith(_GITHUB_HOSTED_PREFIXES) for label in labels
    )


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
