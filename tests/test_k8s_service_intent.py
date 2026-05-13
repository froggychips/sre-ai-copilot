"""Тесты на новый K8sService.execute_intent (PR #3 executor track).

K8sService теперь:
  1. Маппит ActionType → (verb, resource) для guard-валидации структурно,
     а не через парсинг kubectl-строки. Исправляет pre-existing баг с
     `kubectl rollout restart` (cmd_parts[2]="restart" не в ALLOWED_RESOURCES).
  2. Блокирует dry_run=False при SAFE_MODE+APPROVAL_REQUIRED, кроме случая
     post_approval=True (рукоприкладство утверждено).
"""
from unittest.mock import MagicMock, patch


from app.core.execution_dsl import ActionType, ExecutionIntent
from app.services.k8s_service import K8sService, _intent_to_operation


def _restart_intent(ns: str = "squad-1") -> ExecutionIntent:
    return ExecutionIntent(
        action=ActionType.RESTART_DEPLOYMENT,
        resource_type="deployment",
        resource_name="town-service",
        namespace=ns,
        params={},
        risk="low",
    )


def test_intent_to_operation_maps_restart_to_patch_deployments():
    op = _intent_to_operation(_restart_intent())
    assert op.verb == "patch"
    assert op.resource == "deployments"
    assert op.namespace == "squad-1"


def test_intent_to_operation_maps_get_logs_to_get_pods():
    intent = ExecutionIntent(
        action=ActionType.GET_LOGS,
        resource_type="pod",
        resource_name="town-abc",
        namespace="squad-1",
    )
    op = _intent_to_operation(intent)
    assert op.verb == "get"
    assert op.resource == "pods"


def test_intent_to_operation_describe_uses_intent_resource_type():
    intent = ExecutionIntent(
        action=ActionType.DESCRIBE_RESOURCE,
        resource_type="service",
        resource_name="api",
        namespace="squad-2",
    )
    op = _intent_to_operation(intent)
    assert op.verb == "get"
    assert op.resource == "services"


def test_execute_intent_dry_run_passes_through_guard_and_subprocess():
    """dry_run=True → нет SAFE_MODE-блокировки, kubectl вызывается."""
    svc = K8sService()
    intent = _restart_intent()
    fake_proc = MagicMock(returncode=0, stdout="ok", stderr="")
    with patch("app.services.k8s_service.subprocess.run", return_value=fake_proc) as mock_run:
        result = svc.execute_intent(intent, dry_run=True)
    assert result["success"] is True
    assert result["dry_run"] is True
    # --dry-run=server добавлен в команду
    called_cmd = mock_run.call_args[0][0]
    assert "--dry-run=server" in called_cmd


def test_execute_intent_blocks_real_write_without_post_approval():
    """SAFE_MODE + APPROVAL_REQUIRED + dry_run=False + post_approval=False → отказ."""
    svc = K8sService()
    svc.safe_mode = True
    intent = _restart_intent()
    with patch("app.services.k8s_service.settings") as mock_settings:
        mock_settings.APPROVAL_REQUIRED = True
        result = svc.execute_intent(intent, dry_run=False, post_approval=False)
    assert result["success"] is False
    assert "SAFE_MODE" in result["error"]


def test_execute_intent_allows_real_write_with_post_approval():
    """post_approval=True → SAFE_MODE-гейт обходится, kubectl вызывается без --dry-run."""
    svc = K8sService()
    svc.safe_mode = True
    intent = _restart_intent()
    fake_proc = MagicMock(returncode=0, stdout="restarted", stderr="")
    with patch("app.services.k8s_service.settings") as mock_settings, \
         patch("app.services.k8s_service.subprocess.run", return_value=fake_proc) as mock_run:
        mock_settings.APPROVAL_REQUIRED = True
        result = svc.execute_intent(intent, dry_run=False, post_approval=True)
    assert result["success"] is True
    called_cmd = mock_run.call_args[0][0]
    assert "--dry-run" not in " ".join(called_cmd)


def test_execute_intent_returns_kubectl_binary_missing_gracefully():
    """Локально/CI без kubectl — FileNotFoundError ловится."""
    svc = K8sService()
    intent = _restart_intent()
    with patch("app.services.k8s_service.subprocess.run", side_effect=FileNotFoundError("kubectl")):
        result = svc.execute_intent(intent, dry_run=True)
    assert result["success"] is False
    assert result["error"] == "kubectl_binary_missing"


def test_execute_intent_blocked_by_guard_returns_structured_error():
    """Forbidden namespace отлавливается pydantic-валидатором на этапе intent,
    но дополнительно K8sSecurityGuard блокирует write вне squad-* / разрешённых ns.
    """
    svc = K8sService()
    # preprod = READ_ONLY_NAMESPACES tier — write должен быть запрещён.
    intent = ExecutionIntent(
        action=ActionType.RESTART_DEPLOYMENT,
        resource_type="deployment",
        resource_name="x",
        namespace="prod-kingdom1",
        params={},
        risk="low",
    )
    result = svc.execute_intent(intent, dry_run=True)
    assert result["success"] is False
    assert result["error"].startswith("GUARDRAIL_BLOCK")
