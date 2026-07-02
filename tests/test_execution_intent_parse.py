"""Тесты на ExecutionIntent.from_llm_response.

Парсер должен корректно вытаскивать JSON из реальных LLM-ответов:
  - чистый JSON
  - JSON в ```json``` / ``` ``` fence-блоке
  - JSON с prose-обёрткой
  - валидационные сбои (FORBIDDEN_NAMESPACES, схема) → None, без exception
"""
import pytest
from pydantic import ValidationError

from app.core.execution_dsl import ActionType, ExecutionIntent


def test_parse_plain_json():
    text = (
        '{"action": "restart_deployment", "resource_type": "deployment", '
        '"resource_name": "town-service", "namespace": "squad-1", '
        '"params": {}, "risk": "low"}'
    )
    intent = ExecutionIntent.from_llm_response(text)
    assert intent is not None
    assert intent.action == ActionType.RESTART_DEPLOYMENT
    assert intent.resource_name == "town-service"
    assert intent.namespace == "squad-1"
    assert intent.risk == "low"


def test_parse_json_in_code_fence():
    text = (
        "```json\n"
        '{"action": "scale_deployment", "resource_type": "deployment", '
        '"resource_name": "api", "namespace": "preprod-kingdom1", '
        '"params": {"replicas": 3}, "risk": "medium"}\n'
        "```"
    )
    intent = ExecutionIntent.from_llm_response(text)
    assert intent is not None
    assert intent.action == ActionType.SCALE_DEPLOYMENT
    assert intent.params["replicas"] == 3


def test_parse_json_in_bare_fence():
    text = (
        "```\n"
        '{"action": "get_logs", "resource_type": "pod", '
        '"resource_name": "auth-7d4f", "namespace": "squad-2"}\n'
        "```"
    )
    intent = ExecutionIntent.from_llm_response(text)
    assert intent is not None
    assert intent.action == ActionType.GET_LOGS


def test_parse_json_with_prose_prefix():
    """LLM добавил леад-текст перед JSON-объектом — fallback вытаскивает первый {...}."""
    text = (
        "Recommended action based on facts:\n\n"
        '{"action": "describe_resource", "resource_type": "deployment", '
        '"resource_name": "notificator", "namespace": "squad-3"}\n\n'
        "Run this before any restart."
    )
    intent = ExecutionIntent.from_llm_response(text)
    assert intent is not None
    assert intent.action == ActionType.DESCRIBE_RESOURCE


def test_parse_rejects_forbidden_namespace():
    """FORBIDDEN_NAMESPACES (kube-system и т.д.) → None, не исключение."""
    text = (
        '{"action": "restart_deployment", "resource_type": "deployment", '
        '"resource_name": "kube-dns", "namespace": "kube-system", '
        '"params": {}, "risk": "high"}'
    )
    intent = ExecutionIntent.from_llm_response(text)
    assert intent is None


def test_parse_rejects_invalid_action():
    text = (
        '{"action": "drop_table", "resource_type": "deployment", '
        '"resource_name": "x", "namespace": "squad-1"}'
    )
    intent = ExecutionIntent.from_llm_response(text)
    assert intent is None


def test_parse_rejects_invalid_resource_type():
    text = (
        '{"action": "get_logs", "resource_type": "node", '
        '"resource_name": "k8s-worker-1", "namespace": "squad-1"}'
    )
    intent = ExecutionIntent.from_llm_response(text)
    assert intent is None


def test_parse_returns_none_on_empty():
    assert ExecutionIntent.from_llm_response("") is None
    assert ExecutionIntent.from_llm_response("   \n  ") is None


def test_parse_returns_none_on_pure_prose():
    """LLM проигнорировал JSON-инструкцию — возвращаем None, не падаем."""
    text = "Restart the deployment town-service. This should resolve the OOM."
    intent = ExecutionIntent.from_llm_response(text)
    assert intent is None


def test_parse_returns_none_on_invalid_json():
    text = '{"action": "restart_deployment", "resource_name":'  # broken JSON
    intent = ExecutionIntent.from_llm_response(text)
    assert intent is None


# ── namespace charset validator (flag-инъекция) ───────────────────────────────


def _intent(namespace: str) -> ExecutionIntent:
    return ExecutionIntent(
        action=ActionType.RESTART_DEPLOYMENT,
        resource_type="deployment",
        resource_name="town-service",
        namespace=namespace,
    )


def test_namespace_rejects_flag_injection():
    """`squad-1 -n kube-system` не должен пролезть мимо guard'а через namespace."""
    with pytest.raises(ValidationError):
        _intent("squad-1 -n kube-system")


@pytest.mark.parametrize(
    "bad_ns",
    [
        "squad-1 -n kube-system",  # пробел + флаг
        "-nkube-system",           # ведущий '-'
        "squad 1",                 # пробел
        "squad;rm",                # спецсимвол
        "--namespace=kube-system",
    ],
)
def test_namespace_charset_rejects_bad(bad_ns: str):
    with pytest.raises(ValidationError):
        _intent(bad_ns)


@pytest.mark.parametrize("good_ns", ["squad-1", "default", "preprod-kingdom1", "squad-gd"])
def test_namespace_charset_accepts_valid(good_ns: str):
    assert _intent(good_ns).namespace == good_ns


def test_namespace_default_still_valid():
    """Дефолт "default" обязан проходить charset-валидацию."""
    intent = ExecutionIntent(
        action=ActionType.GET_PODS,
        resource_type="pod",
        resource_name="anything",
    )
    assert intent.namespace == "default"


def test_namespace_forbidden_still_blocked():
    """FORBIDDEN-контроль сохраняется поверх charset."""
    with pytest.raises(ValidationError):
        _intent("kube-system")


def test_from_llm_response_rejects_flag_injection_namespace():
    """Через from_llm_response flag-инъекция в namespace → None (не exception)."""
    text = (
        '{"action": "restart_deployment", "resource_type": "deployment", '
        '"resource_name": "town-service", "namespace": "squad-1 -n kube-system"}'
    )
    assert ExecutionIntent.from_llm_response(text) is None
