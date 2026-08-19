"""Действие описано в одном месте — реестре ActionSpec.

Раньше семантика жила в трёх: `ActionType` (перечень), `DSLTranslator` (как
выполнить) и `executor_gate` (мутирующее ли, какой command_kind, какие поля
проверять). Связи между ними не было: добавив действие в перечень и
транслятор, про guard можно было забыть — и новое мутирующее действие
прошло бы проверку риска как безвредное чтение.

Тест ловил такой рассинхрон постфактум. Реестр убирает саму возможность:
`ACTION_SPECS` проверяется на импорте, а `action_spec()` бросает на
неописанном действии вместо того, чтобы считать его безопасным.

Здесь остались проверки самих свойств реестра — что он полон, что
классификация осмысленна, и что argv по-прежнему защищает от инъекции.
"""
import pytest

from app.core.execution_dsl import (ACTION_SPECS, ActionType,
                                    ConcurrencyPolicy, DSLTranslator,
                                    ExecutionIntent, action_spec, is_mutating)

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


def test_registry_agrees_with_the_expected_classification():
    """Реестр — источник истины; список выше сторожит его от тихой правки."""
    for action in MUTATING:
        assert is_mutating(action), f"{action.value} перестало быть мутирующим"
    for action in READ_ONLY:
        assert not is_mutating(action), f"{action.value} стало мутирующим"


def test_every_action_has_a_spec():
    """Действие без спецификации не должно существовать.

    Полнота проверяется и на импорте модуля — здесь дублируем ради понятного
    сообщения в отчёте тестов.
    """
    missing = [a.value for a in ActionType if a not in ACTION_SPECS]
    assert not missing, f"действия без ActionSpec: {missing}"


def test_unknown_action_is_refused_not_assumed_safe():
    """Отсутствие спецификации — повод отказаться, а не счесть безвредным."""
    class Fake(str):
        pass

    with pytest.raises(ValueError, match="ActionSpec"):
        action_spec(Fake("delete_everything"))


def test_mutating_actions_declare_a_command_kind():
    """command_kind кормит risk_axes: без него риск считается вслепую."""
    for action in MUTATING:
        assert action_spec(action).command_kind, (
            f"{action.value} мутирует, но не сообщает command_kind"
        )


def test_read_only_actions_have_no_command_kind():
    """У чтения нет вида команды — иначе риск завышается на пустом месте."""
    for action in READ_ONLY:
        assert action_spec(action).command_kind is None


# --- concurrency policy на каждое мутирующее действие ---------------------


def test_every_mutating_action_declares_concurrency_policy():
    """«Забыли подумать» и «защиты нет» должны выглядеть по-разному.

    Читающие действия — NONE. Мутирующие обязаны выбрать: PRECONDITION, если
    apiserver может проверить состояние сам, или UNGUARDED — но это осознанное
    признание, а не умолчание.
    """
    for action in MUTATING:
        policy = action_spec(action).concurrency
        assert policy in (ConcurrencyPolicy.PRECONDITION,
                          ConcurrencyPolicy.UNGUARDED), (
            f"{action.value}: политика {policy} не годится для мутации"
        )


def test_read_only_actions_need_no_concurrency_policy():
    for action in READ_ONLY:
        assert action_spec(action).concurrency is ConcurrencyPolicy.NONE


def test_scale_uses_a_precondition_not_a_promise():
    """У scale precondition возможен — и обязан быть выбран."""
    assert action_spec(
        ActionType.SCALE_DEPLOYMENT
    ).concurrency is ConcurrencyPolicy.PRECONDITION


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
