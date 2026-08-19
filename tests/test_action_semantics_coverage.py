"""Каждое действие должно быть известно и транслятору, и guard'у.

Семантика действия сегодня живёт в трёх местах:

  * `ActionType` — перечень;
  * `DSLTranslator.to_argv` — как выполнить;
  * `executor_gate` — мутирующее оно или читающее (`_COMMAND_KIND` и условие
    рядом с ним).

Первое связано со вторым явно — незнакомое действие роняет `ValueError`.
А вот забыть про третье можно молча: новое мутирующее действие тогда пройдёт
через gate как безвредное чтение. Тестов на это не было.
"""
import pytest

from app.core.execution_dsl import ActionType, DSLTranslator, ExecutionIntent
from app.remediation.executor_gate import _COMMAND_KIND

#: Действия, меняющие состояние кластера. Список ведётся здесь намеренно:
#: чтобы добавить сюда новое действие, о его последствиях нужно подумать
#: отдельно, а не унаследовать «неизвестно → безопасно».
MUTATING = {
    ActionType.RESTART_DEPLOYMENT,
    ActionType.SCALE_DEPLOYMENT,
}

READ_ONLY = {
    ActionType.GET_LOGS,
    ActionType.DESCRIBE_RESOURCE,
    ActionType.GET_PODS,
}


def _intent(action: ActionType, **params) -> ExecutionIntent:
    return ExecutionIntent(
        action=action,
        resource_type="deployment",
        resource_name="town-service",
        namespace="squad-1",
        params=params,
    )


def test_every_action_is_classified():
    """Ни одно действие не должно остаться без ответа «оно мутирующее?»."""
    unclassified = set(ActionType) - MUTATING - READ_ONLY
    assert not unclassified, (
        f"действия без классификации: {sorted(a.value for a in unclassified)}. "
        "Реши явно: меняет оно кластер или только читает."
    )


def test_every_mutating_action_is_known_to_the_gate():
    """`_COMMAND_KIND` кормит risk_axes — без записи риск считается вслепую."""
    missing = [a.value for a in MUTATING if a not in _COMMAND_KIND]
    assert not missing, (
        f"мутирующие действия неизвестны executor_gate: {missing}. "
        "Они пройдут проверку риска как обычное чтение."
    )


def test_read_only_actions_are_not_marked_as_commands():
    """Чтение не должно попадать в реестр мутаций — иначе риск завышен."""
    extra = [a.value for a in READ_ONLY if a in _COMMAND_KIND]
    assert not extra, f"читающие действия помечены как мутации: {extra}"


@pytest.mark.parametrize("action", list(ActionType))
def test_every_action_translates_to_argv(action):
    """Транслятор обязан знать каждое действие перечня."""
    argv = DSLTranslator.to_argv(_intent(action, replicas=2))
    assert argv and argv[0] == "kubectl"


@pytest.mark.parametrize("action", list(ActionType))
def test_string_form_matches_argv(action):
    """Строка показывается человеку, argv выполняется — они обязаны совпадать.

    Разъехавшись, они дадут худший вид ошибки: в превью одно, в кластере
    другое.
    """
    intent = _intent(action, replicas=2)
    assert DSLTranslator.to_kubectl(intent) == " ".join(
        DSLTranslator.to_argv(intent)
    )


# --- argv против инъекции флагов ------------------------------------------


def test_charset_validator_rejects_space_in_label():
    """Первый рубеж: значение с пробелом не проходит валидацию вовсе."""
    with pytest.raises(Exception):
        _intent(ActionType.GET_PODS, label="app=a b")


def test_argv_keeps_a_dangerous_value_as_one_argument():
    """Второй рубеж — на случай поля, у которого валидатор забыли.

    `model_construct` намеренно обходит валидацию: так выглядит будущее поле,
    добавленное без charset-проверки. При старой схеме (f-строка →
    `command.split()`) значение стало бы отдельными аргументами и подменило
    namespace. В argv оно остаётся одним элементом, что бы в нём ни было.
    """
    intent = ExecutionIntent.model_construct(
        action=ActionType.GET_PODS,
        resource_type="deployment",
        resource_name="town-service",
        namespace="squad-1",
        params={"label": "app=a -n kube-system"},
        risk="medium",
    )
    argv = DSLTranslator.to_argv(intent)

    assert "app=a -n kube-system" in argv, "значение расщепилось на аргументы"
    assert argv.count("-n") == 1, "подменённый namespace попал в команду"
    assert argv[argv.index("-n") + 1] == "squad-1"


def test_namespace_reaches_kubectl_as_a_single_token():
    intent = _intent(ActionType.GET_PODS)
    argv = DSLTranslator.to_argv(intent)
    assert argv[argv.index("-n") + 1] == "squad-1"


# --- optimistic concurrency ------------------------------------------------


def test_scale_carries_current_replicas_as_precondition():
    """Между preview и apply реплики мог поменять кто-то ещё — HPA, человек.

    `--current-replicas` превращает это в отказ на стороне apiserver вместо
    молчаливой перезаписи чужого решения.
    """
    argv = DSLTranslator.to_argv(
        _intent(ActionType.SCALE_DEPLOYMENT, replicas=5, current_replicas=2)
    )
    assert "--current-replicas=2" in argv
    assert "--replicas=5" in argv


def test_scale_without_known_current_state_has_no_precondition():
    """Не знаем текущее число реплик — не выдумываем его."""
    argv = DSLTranslator.to_argv(_intent(ActionType.SCALE_DEPLOYMENT, replicas=5))
    assert not [a for a in argv if a.startswith("--current-replicas")]


# --- строковый путь исполнения закрыт навсегда ----------------------------
#
# `_run_kubectl` раньше принимал `command: str` и восстанавливал аргументы
# через `command.split()`. К 19.08.2026 у него остался единственный
# вызывающий, уже передававший argv, — то есть fallback стал мёртвым кодом,
# существующим только как путь атаки. Достаточно одного нового вызывающего,
# собравшего команду строкой, чтобы он ожил.


def test_executor_never_splits_a_string_into_argv():
    """В исполнителе не должно остаться расщепления строки на аргументы."""
    import pathlib
    src = (pathlib.Path(__file__).parent.parent
           / "app" / "services" / "k8s_service.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in src.splitlines()
        if not line.lstrip().startswith("#")
    )
    # Докстринг объясняет историю и содержит `command.split()` текстом —
    # ищем именно исполняемое присваивание.
    assert "= command.split()" not in code, (
        "строковый путь исполнения вернулся: argv снова собирается split(), "
        "и charset-валидаторы опять становятся единственной защитой"
    )


def test_run_kubectl_requires_argv_list():
    """Сигнатура обязана принимать список, а не строку."""
    import inspect

    from app.services.k8s_service import K8sService

    sig = inspect.signature(K8sService._run_kubectl)
    assert "argv" in sig.parameters, "argv перестал быть параметром"
    assert "command" not in sig.parameters, (
        "строковый параметр вернулся — вместе с ним вернётся и split()"
    )
