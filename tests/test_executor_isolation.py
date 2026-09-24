"""Запись в кластер — только у copilot-executor (EXECUTOR_DISPATCH=queue).

До 24.09.2026 apply шёл из api-пода, dry-run — из worker-а, оба под общим SA
`sre-ai`; write-роль, привязанная к нему, доставалась и поду, принимающему
вебхуки из интернета. Здесь держим обе половины разделения:

* **Код.** При queue api и worker только кладут задачу в очередь executor и
  сами kubectl не зовут; задачи исполняют прежние apply_intent/execute_intent
  (все проверки — там), отказ очереди или таймаут — fail-closed.
* **Манифесты.** write-роль привязана только к sre-ai-executor; этот SA
  носит только copilot-executor; api/worker выставлены в queue, сам executor —
  в inline; NetworkPolicy его покрывает; deploy.sh поднимает его первым.
  Ошибка в любом из этих мест не ломает рендер и в диффе глазом не видна.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from app.api import discord_interactions
from app.core.execution_dsl import ActionType, ExecutionIntent
from app.workers import executor_tasks
from app.workers.pipeline import IncidentPipeline

_REPO_ROOT = Path(__file__).resolve().parents[1]
_K8S = _REPO_ROOT / "k8s"
_SIG = "abc123def456"
_WRITE_VERBS = {"create", "update", "patch", "delete", "deletecollection", "*"}


def _docs(path: Path) -> list:
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if isinstance(d, dict)]


def _all_k8s_docs() -> list:
    out = []
    for path in sorted(_K8S.rglob("*.yaml")):
        out.extend(_docs(path))
    return out


def _workloads() -> dict:
    return {
        d["metadata"]["name"]: d
        for d in _all_k8s_docs()
        if d.get("kind") in {"Deployment", "StatefulSet", "DaemonSet"}
    }


def _env(workload: dict) -> dict:
    container = workload["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value") for e in container.get("env") or []}


def _intent() -> ExecutionIntent:
    return ExecutionIntent(
        action=ActionType.RESTART_DEPLOYMENT,
        resource_type="deployment",
        resource_name="town-service",
        namespace="squad-1",
        params={},
        risk="low",
    )


# ─── Манифесты ─────────────────────────────────────────────────────────────


def test_write_role_is_bound_only_to_executor_sa() -> None:
    """Ни один binding (включая закомментированный пример) не даёт
    sre-ai-remediate SA приложения; ClusterRole-и, привязанные к sre-ai, —
    без write-глаголов."""
    rbac_text = (_K8S / "base" / "rbac.yaml").read_text(encoding="utf-8")
    docs = _docs(_K8S / "base" / "rbac.yaml")
    roles = {d["metadata"]["name"]: d for d in docs if d["kind"] in {"Role", "ClusterRole"}}

    for d in docs:
        if d["kind"] not in {"RoleBinding", "ClusterRoleBinding"}:
            continue
        subjects = {s["name"] for s in d.get("subjects") or []}
        role = roles.get(d["roleRef"]["name"])
        if d["roleRef"]["name"] == "sre-ai-remediate":
            assert subjects == {"sre-ai-executor"}, d["metadata"]["name"]
        if "sre-ai" in subjects and role is not None:
            for rule in role.get("rules") or []:
                assert not (_WRITE_VERBS & set(rule["verbs"])), (
                    f"{d['metadata']['name']} даёт sre-ai запись: {rule}"
                )

    # Закомментированный пример write-binding-а — то, что копируют руками.
    example = rbac_text[rbac_text.index("# Пример write-binding"):]
    example = example[:example.index("\n---")]
    assert "#   name: sre-ai-executor" in example
    assert "#   name: sre-ai\n" not in example


def test_remediate_role_matches_what_execution_dsl_can_do() -> None:
    """Только patch/update deployments(+scale): exec и delete pods сняты —
    их не вызывает ни один путь кода, а exec под write-SA = произвольная
    команда в любом контейнере namespace-а."""
    role = next(d for d in _docs(_K8S / "base" / "rbac.yaml")
                if d["kind"] == "Role" and d["metadata"]["name"] == "sre-ai-remediate")
    resources = {r for rule in role["rules"] for r in rule["resources"]}
    assert resources == {"deployments", "deployments/scale"}


def test_executor_sa_is_used_only_by_copilot_executor() -> None:
    users = {
        name for name, w in _workloads().items()
        if w["spec"]["template"]["spec"].get("serviceAccountName") == "sre-ai-executor"
    }
    assert users == {"copilot-executor"}


def test_executor_deployment_listens_only_to_executor_queue() -> None:
    w = _workloads()["copilot-executor"]
    cmd = w["spec"]["template"]["spec"]["containers"][0]["command"]
    assert cmd[cmd.index("-Q") + 1] == "executor"
    # Сам executor исполняет inline, а не ставит задачи себе же.
    assert _env(w)["EXECUTOR_DISPATCH"] == "inline"
    # Проба смотрит в свой нод (-n executor@%h), а не в celery@ по умолчанию.
    probe = " ".join(w["spec"]["template"]["spec"]["containers"][0]
                     ["livenessProbe"]["exec"]["command"])
    assert '-d "executor@' in probe
    assert cmd[cmd.index("-n") + 1] == "executor@%h"


@pytest.mark.parametrize("name", ["sre-ai-api", "copilot-worker"])
def test_api_and_worker_dispatch_to_queue_under_read_only_sa(name: str) -> None:
    w = _workloads()[name]
    assert w["spec"]["template"]["spec"]["serviceAccountName"] == "sre-ai"
    assert _env(w)["EXECUTOR_DISPATCH"] == "queue"


def test_network_policy_covers_executor() -> None:
    policy = next(d for d in _docs(_K8S / "networkpolicy.yaml")
                  if d["metadata"]["name"] == "copilot-network-policy")
    values = policy["spec"]["podSelector"]["matchExpressions"][0]["values"]
    assert "copilot-executor" in values


def test_deploy_sh_applies_executor_before_its_producers() -> None:
    text = (_REPO_ROOT / "deploy.sh").read_text(encoding="utf-8")
    ex = text.index("apply_with_image k8s/executor.yaml")
    assert ex < text.index("apply_with_image k8s/base/deployment.yaml")
    assert ex < text.index("apply_with_image k8s/worker.yaml")
    assert "copilot-executor" in text[text.index("for d in "):].splitlines()[0]


# ─── Конфиг ────────────────────────────────────────────────────────────────


def test_executor_dispatch_typo_is_rejected() -> None:
    """«Queue» не должна молча стать inline — тогда apply снова шёл бы из api."""
    from app.config import Settings
    with pytest.raises(ValueError, match="EXECUTOR_DISPATCH"):
        Settings(EXECUTOR_DISPATCH="Queue", LLM_BACKEND="claude_cli")


# ─── Discord: apply_confirm и approve ──────────────────────────────────────


def _request(payload: dict) -> MagicMock:
    request = MagicMock()
    request.body = AsyncMock(return_value=json.dumps(payload).encode())
    return request


def _confirm_payload(token: str = "tok-abc") -> dict:
    return {
        "type": 3,
        "data": {"custom_id": f"apply_confirm:inc-q:{_SIG}"},
        "member": {"user": {"id": "user-42", "username": "operator"}},
        "token": token,
    }


def _fake_record(incident_id, sig, status, approved_by):
    return {"already_decided": False, "status": status,
            "approved_by": approved_by, "decided_at": "12:00 UTC"}


@pytest.mark.asyncio
async def test_apply_confirm_queue_dispatches_to_executor_not_inline() -> None:
    """queue: задача уходит в очередь вместе с interaction token (followup
    отправит executor), фоновой таски с kubectl в api нет."""
    with patch.object(discord_interactions, "_verify_signature", return_value=True), \
         patch.object(discord_interactions.settings, "DISCORD_PUBLIC_KEY", "deadbeef"), \
         patch.object(discord_interactions, "_is_authorized_approver", return_value=(True, "ok")), \
         patch.object(discord_interactions, "_record_decision", side_effect=_fake_record), \
         patch.object(discord_interactions.settings, "EXECUTOR_APPROVAL_ENABLED", True), \
         patch.object(executor_tasks.settings, "EXECUTOR_DISPATCH", "queue"), \
         patch.object(executor_tasks, "dispatch_apply", return_value="task-1") as dispatch, \
         patch.object(discord_interactions, "_spawn_background_task") as spawn:
        resp = await discord_interactions.discord_interactions(
            _request(_confirm_payload()), x_signature_ed25519="00" * 64, x_signature_timestamp="0",
        )

    assert resp["type"] == 5
    dispatch.assert_called_once_with("inc-q", "user-42", _SIG, "tok-abc")
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_apply_confirm_queue_broker_down_answers_immediately() -> None:
    """Брокер недоступен → задачи нет; ответ сразу, а не вечный loader."""
    with patch.object(discord_interactions, "_verify_signature", return_value=True), \
         patch.object(discord_interactions.settings, "DISCORD_PUBLIC_KEY", "deadbeef"), \
         patch.object(discord_interactions, "_is_authorized_approver", return_value=(True, "ok")), \
         patch.object(discord_interactions, "_record_decision", side_effect=_fake_record), \
         patch.object(discord_interactions.settings, "EXECUTOR_APPROVAL_ENABLED", True), \
         patch.object(executor_tasks.settings, "EXECUTOR_DISPATCH", "queue"), \
         patch.object(executor_tasks, "dispatch_apply", side_effect=ConnectionError("redis")), \
         patch.object(discord_interactions, "_spawn_background_task") as spawn:
        resp = await discord_interactions.discord_interactions(
            _request(_confirm_payload()), x_signature_ed25519="00" * 64, x_signature_timestamp="0",
        )

    assert resp["type"] == 4
    assert "kubectl не запущен" in resp["data"]["content"]
    assert "ConnectionError" in resp["data"]["content"]
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_approve_queue_dispatches_to_executor(monkeypatch) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base
    from app.knowledge_graph import schema  # noqa: F401
    from app.services.intent_signature import compute_signature

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(discord_interactions, "SessionLocal",
                        sessionmaker(bind=engine, autocommit=False, autoflush=False))
    sig = compute_signature(_intent())
    payload = {
        "type": 3,
        "data": {"custom_id": f"approve:inc-A:{sig}"},
        "member": {"user": {"id": "u-1", "username": "operator"}},
        "token": "tok-abc",
        "message": {"id": "m-1", "channel_id": "c-1",
                    "embeds": [{"title": "t", "footer": {"text": "incident/inc-A"}}]},
    }

    def _close(coro):
        if hasattr(coro, "close"):
            coro.close()
        import asyncio
        f = asyncio.get_running_loop().create_future()
        f.set_result(None)
        return f

    with patch.object(discord_interactions, "_verify_signature", return_value=True), \
         patch.object(discord_interactions.settings, "DISCORD_PUBLIC_KEY", "deadbeef"), \
         patch.object(discord_interactions.settings, "EXECUTOR_ENABLED", True), \
         patch.object(discord_interactions.settings, "EXECUTOR_APPROVAL_ENABLED", True), \
         patch.object(discord_interactions.settings, "DISCORD_APPROVERS_USER_IDS", "u-1"), \
         patch.object(discord_interactions.audit_service, "log_event"), \
         patch.object(executor_tasks.settings, "EXECUTOR_DISPATCH", "queue"), \
         patch.object(executor_tasks, "dispatch_apply", return_value="task-2") as dispatch, \
         patch("asyncio.create_task", side_effect=_close):
        resp = await discord_interactions.discord_interactions(
            _request(payload), x_signature_ed25519="00" * 64, x_signature_timestamp="0",
        )

    dispatch.assert_called_once_with("inc-A", "operator", sig)
    assert "Executor launched" in resp["data"]["content"]


# ─── Стадия пайплайна: dry-run ─────────────────────────────────────────────


def _pipeline() -> IncidentPipeline:
    pl = IncidentPipeline.__new__(IncidentPipeline)
    pl.incident_id = "smoke-q"
    pl.execution_intent = _intent()
    pl.executor_result = None
    pl.traces = []
    pl.root_span = MagicMock()
    return pl


@pytest.mark.asyncio
async def test_stage_executor_queue_runs_dry_run_in_executor_not_worker() -> None:
    pl = _pipeline()
    with patch("app.workers.pipeline.settings.EXECUTOR_ENABLED", True), \
         patch.object(executor_tasks.settings, "EXECUTOR_DISPATCH", "queue"), \
         patch.object(executor_tasks, "run_dry_run_via_queue",
                      return_value={"success": True, "command": "kubectl rollout restart",
                                    "stdout": "ok", "exit_code": 0}) as via_queue, \
         patch("app.services.k8s_service.k8s_service.execute_intent") as inline:
        await pl.stage_executor()

    assert pl.executor_result["status"] == "dry_run_ok"
    via_queue.assert_called_once_with(_intent().model_dump(mode="json"))
    inline.assert_not_called()


@pytest.mark.asyncio
async def test_stage_executor_queue_timeout_is_fail_closed() -> None:
    """Executor не поднят → таймаут → status=error, кнопки Apply не будет."""
    from celery.exceptions import TimeoutError as CeleryTimeout

    pl = _pipeline()
    with patch("app.workers.pipeline.settings.EXECUTOR_ENABLED", True), \
         patch.object(executor_tasks.settings, "EXECUTOR_DISPATCH", "queue"), \
         patch.object(executor_tasks, "run_dry_run_via_queue", side_effect=CeleryTimeout("60s")), \
         patch("app.workers.pipeline.audit_service.log_event"):
        await pl.stage_executor()

    assert pl.executor_result["status"] == "error"
    assert pl.executor_result["error_type"] == "TimeoutError"


# ─── Задачи executor-а ─────────────────────────────────────────────────────


def test_executor_tasks_registered_and_apply_is_at_most_once() -> None:
    from app.workers.tasks import celery_app

    apply_task = celery_app.tasks[executor_tasks.TASK_EXECUTOR_APPLY]
    assert executor_tasks.TASK_EXECUTOR_DRY_RUN in celery_app.tasks
    # Переотправка после смерти executor-а посреди kubectl = повторная запись.
    assert apply_task.acks_late is False
    assert apply_task.reject_on_worker_lost is False


def test_executor_apply_task_runs_apply_intent_and_sends_followup() -> None:
    outcome = {"ok": True, "result": {"success": True, "command": "kubectl scale",
                                      "stdout": "scaled"}}
    with patch("app.services.executor_apply.apply_intent", return_value=outcome) as apply, \
         patch("app.api.discord_interactions._send_followup", new_callable=AsyncMock) as followup:
        res = executor_tasks.executor_apply_task.run("inc-1", "user-42", _SIG, "tok-abc")

    apply.assert_called_once_with("inc-1", "user-42", _SIG)
    token, content = followup.await_args.args
    assert token == "tok-abc"
    assert "✅" in content and "kubectl scale" in content
    assert res == {"ok": True, "success": True, "command": "kubectl scale"}


def test_executor_apply_task_without_token_sends_nothing() -> None:
    with patch("app.services.executor_apply.apply_intent",
               return_value={"ok": False, "reason": "gate_blocked"}), \
         patch("app.api.discord_interactions._send_followup", new_callable=AsyncMock) as followup:
        res = executor_tasks.executor_apply_task.run("inc-1", "operator", _SIG)

    followup.assert_not_awaited()
    assert res == {"ok": False, "reason": "gate_blocked"}


def test_executor_apply_task_crash_is_audited_and_reported() -> None:
    with patch("app.services.executor_apply.apply_intent", side_effect=RuntimeError("boom")), \
         patch("app.services.audit_logger.audit_service.log_event") as audit, \
         patch("app.api.discord_interactions._send_followup", new_callable=AsyncMock) as followup:
        res = executor_tasks.executor_apply_task.run("inc-1", "user-42", _SIG, "tok-abc")

    assert res["ok"] is False and res["reason"] == "execute_error"
    assert audit.call_args.args[0] == "DISCORD_BACKGROUND_TASK_FAILED"
    followup.assert_awaited_once()


def test_executor_dry_run_task_revalidates_intent_and_runs_dry_run() -> None:
    with patch("app.services.k8s_service.k8s_service.execute_intent",
               return_value={"success": True}) as execute:
        executor_tasks.executor_dry_run_task.run(_intent().model_dump(mode="json"))

    intent_arg, dry_run = execute.call_args.args
    assert intent_arg == _intent()
    assert dry_run is True


def test_executor_dry_run_task_rejects_malformed_intent() -> None:
    from pydantic import ValidationError
    with patch("app.services.k8s_service.k8s_service.execute_intent") as execute:
        with pytest.raises(ValidationError):
            executor_tasks.executor_dry_run_task.run({"action": "delete_everything"})
    execute.assert_not_called()


def test_run_dry_run_via_queue_uses_executor_queue_timeout_and_forgets() -> None:
    fake = MagicMock()
    fake.get.return_value = {"success": True}
    with patch.object(executor_tasks.executor_dry_run_task, "apply_async", return_value=fake) as aa, \
         patch.object(executor_tasks.settings, "EXECUTOR_QUEUE_NAME", "executor"), \
         patch.object(executor_tasks.settings, "EXECUTOR_DRY_RUN_TIMEOUT_SECONDS", 7):
        assert executor_tasks.run_dry_run_via_queue({"x": 1}) == {"success": True}

    assert aa.call_args.kwargs["queue"] == "executor"
    fake.get.assert_called_once_with(timeout=7, disable_sync_subtasks=False)
    fake.forget.assert_called_once()


def test_dispatch_apply_targets_executor_queue() -> None:
    fake = MagicMock(id="t-9")
    with patch.object(executor_tasks.executor_apply_task, "apply_async", return_value=fake) as aa:
        assert executor_tasks.dispatch_apply("inc-1", "u", _SIG, "tok") == "t-9"
    assert aa.call_args.kwargs == {"args": ["inc-1", "u", _SIG, "tok"],
                                   "queue": executor_tasks.settings.EXECUTOR_QUEUE_NAME}


@pytest.mark.asyncio
async def test_approve_queue_dispatch_failure_keeps_buttons_for_retry(monkeypatch) -> None:
    """Брокер лёг на Approve: одобрение уже записано, повторный Approve упрётся
    в already_decided — значит кнопки снимать нельзя, иначе действие
    пропущено навсегда. Повтор — через Apply → apply_confirm."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base
    from app.knowledge_graph import schema  # noqa: F401
    from app.services.intent_signature import compute_signature

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(discord_interactions, "SessionLocal",
                        sessionmaker(bind=engine, autocommit=False, autoflush=False))
    sig = compute_signature(_intent())
    payload = {
        "type": 3,
        "data": {"custom_id": f"approve:inc-F:{sig}"},
        "member": {"user": {"id": "u-1", "username": "operator"}},
        "token": "tok-abc",
        "message": {"id": "m-1", "channel_id": "c-1",
                    "embeds": [{"title": "t", "footer": {"text": "incident/inc-F"}}]},
    }

    with patch.object(discord_interactions, "_verify_signature", return_value=True), \
         patch.object(discord_interactions.settings, "DISCORD_PUBLIC_KEY", "deadbeef"), \
         patch.object(discord_interactions.settings, "EXECUTOR_ENABLED", True), \
         patch.object(discord_interactions.settings, "EXECUTOR_APPROVAL_ENABLED", True), \
         patch.object(discord_interactions.settings, "DISCORD_APPROVERS_USER_IDS", "u-1"), \
         patch.object(discord_interactions.audit_service, "log_event"), \
         patch.object(executor_tasks.settings, "EXECUTOR_DISPATCH", "queue"), \
         patch.object(executor_tasks, "dispatch_apply", side_effect=ConnectionError("redis")), \
         patch.object(discord_interactions, "_edit_message_after_decision") as edit:
        resp = await discord_interactions.discord_interactions(
            _request(payload), x_signature_ed25519="00" * 64, x_signature_timestamp="0",
        )

    edit.assert_not_called()
    assert "kubectl не запущен" in resp["data"]["content"]
    assert "Apply" in resp["data"]["content"]


def test_apply_confirm_retries_for_already_approved_action() -> None:
    """Путь повтора, на который ссылается ответ выше: apply_confirm отменяет
    запуск только для решения, отличного от approved."""
    src = (_REPO_ROOT / "app" / "api" / "discord_interactions.py").read_text(encoding="utf-8")
    assert 'if decision["already_decided"] and decision["status"] != "approved":' in src


def test_deploy_sh_write_leak_check_is_per_namespace_and_fatal() -> None:
    """`can-i --all-namespaces` не видит RoleBinding в одном squad-foo — а
    write-роль выдаётся именно так. И найденная утечка валит деплой."""
    text = (_REPO_ROOT / "deploy.sh").read_text(encoding="utf-8")
    block = text[text.index('sre_ai_sa="system:serviceaccount'):]
    block = block[:block.index("# ── 3. Миграции")]
    assert "kubectl get rolebindings -A" in block
    assert '-n "${rb_ns}"' in block
    assert "exit 1" in block
