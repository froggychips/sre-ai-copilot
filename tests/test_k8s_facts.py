
from types import SimpleNamespace

from app.context.k8s_facts import K8sFacts
from app.diagnostics.facts import FactKind
from app.diagnostics.rules.oom import OOMKilledRule
from app.diagnostics.rules.pod_events import PodEventsRule


def test_peer_squad1_returns_squad2():
    assert K8sFacts._peer_namespace("squad-1-kingdom2") == "squad-2-kingdom2"


def test_peer_squad2_returns_squad3():
    assert K8sFacts._peer_namespace("squad-2-shared") == "squad-3-shared"


def test_peer_squad3_returns_squad4():
    assert K8sFacts._peer_namespace("squad-3-auth") == "squad-4-auth"


def test_peer_squad7_shared_returns_squad8():
    assert K8sFacts._peer_namespace("squad-7-shared") == "squad-8-shared"


def test_peer_is_n_plus_one_not_hardcoded():
    # Регрессия: раньше хардкодилось peer=squad-2 для всех (кроме 2→3).
    # Теперь строго N+1, согласовано с docstring.
    assert K8sFacts._peer_namespace("squad-4-payments") == "squad-5-payments"
    assert K8sFacts._peer_namespace("squad-39-kingdom5") == "squad-40-kingdom5"


def test_peer_non_squad_returns_none():
    assert K8sFacts._peer_namespace("prod") is None
    assert K8sFacts._peer_namespace("preprod") is None
    assert K8sFacts._peer_namespace("preupdate") is None
    assert K8sFacts._peer_namespace("kube-system") is None
    assert K8sFacts._peer_namespace("mcp") is None
    assert K8sFacts._peer_namespace("default") is None


def test_peer_squad_no_suffix_returns_none():
    # "squad-1" has no "-suffix" component — should not match
    assert K8sFacts._peer_namespace("squad-1") is None


def test_peer_suffix_preserved_exactly():
    assert (
        K8sFacts._peer_namespace("squad-1-very-long-suffix")
        == "squad-2-very-long-suffix"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Скоупинг снапшота по target-workload + редакция pod-логов
#
# Инцидент: алерт без pod-label в namespace с НЕСВЯЗАННЫМ OOM давал
# «oom_killed observed conf=0.95» — observed-факт критик опровергнуть не
# может, и в Discord уезжала уверенная неверная причина. Источники ложного
# anchor-а: terminated-строки/логи чужих подов в text blob (его сканируют
# text-fallback-и правил) и Warning-события чужих объектов в PodEventsRule.
#
# Плюс PII: blob уходит в LLM-промпт, а pod-логи штатно содержат токены,
# пароли, email-ы и IP.
# ═══════════════════════════════════════════════════════════════════════════

_TARGET_POD = "town-service-7d9f4-abcde"
_FOREIGN_POD = "notificator-55c8b-zzzzz"


def _terminated(reason, exit_code, message=""):
    return SimpleNamespace(reason=reason, exit_code=exit_code, message=message)


def _pod(name, phase="Failed", terminated=None, container="app", restarts=3):
    cs = SimpleNamespace(
        name=container,
        restart_count=restarts,
        last_state=SimpleNamespace(terminated=terminated),
        state=SimpleNamespace(terminated=None),
    )
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        status=SimpleNamespace(phase=phase, container_statuses=[cs]),
        spec=SimpleNamespace(containers=[], node_name="dev-1"),
    )


def _event(reason, obj, message="", count=1):
    return SimpleNamespace(
        type="Warning",
        reason=reason,
        message=message,
        count=count,
        involved_object=SimpleNamespace(name=obj),
        last_timestamp="2026-08-10T10:00:00Z",
        event_time=None,
        first_timestamp=None,
    )


class _FakeCoreV1:
    """Минимальный CoreV1Api: только методы, которые зовёт _collect_sync."""

    def __init__(self, pods, logs=None, events=None):
        self._pods = pods
        self._logs = logs or {}
        self._events = events or []
        self.logs_requested = []

    def list_namespaced_pod(self, namespace):
        return SimpleNamespace(items=self._pods)

    def read_namespaced_pod_log(self, name, namespace, tail_lines=None, previous=False):
        self.logs_requested.append(name)
        if not previous:
            raise RuntimeError("no current logs in fixture")
        if name not in self._logs:
            raise RuntimeError("no previous logs for pod")
        return self._logs[name]

    def list_namespaced_event(self, namespace, field_selector=None):
        if field_selector and field_selector.startswith("involvedObject.name="):
            want = field_selector.split("=", 1)[1]
            return SimpleNamespace(
                items=[e for e in self._events if e.involved_object.name == want]
            )
        return SimpleNamespace(items=list(self._events))


def _install_fake_k8s(monkeypatch, api):
    import app.context.k8s_facts as mod

    monkeypatch.setattr(
        mod, "k8s_config",
        SimpleNamespace(
            load_incluster_config=lambda: None,
            load_kube_config=lambda: None,
        ),
    )
    monkeypatch.setattr(mod, "client", SimpleNamespace(CoreV1Api=lambda: api))
    return api


# ---------------------------------------------------------------------------
# terminated-строки чужого workload-а не создают oom-факт
# ---------------------------------------------------------------------------

def test_foreign_terminated_line_does_not_feed_oom_text_fallback(monkeypatch):
    api = _install_fake_k8s(monkeypatch, _FakeCoreV1(
        pods=[
            _pod(_TARGET_POD, terminated=_terminated("Error", 1)),
            _pod(_FOREIGN_POD, terminated=_terminated("OOMKilled", 137)),
        ],
    ))
    snap = K8sFacts._collect_sync("prod-shared", pod=_TARGET_POD)

    # Blob не содержит regex-бейта чужого пода...
    assert "OOMKilled" not in snap.text
    assert "exit_code=137" not in snap.text
    # ...но сам факт «в ns есть ещё падения» сохранён именем пода.
    assert _FOREIGN_POD in snap.text
    # Structured-состояние остаётся полным: скоупинг делают правила.
    assert set(snap.container_terminated) == {_TARGET_POD, _FOREIGN_POD}
    assert api.logs_requested  # логи target-workload-а запрашивались

    # Text-fallback OOMKilledRule (без k8s_pod_state — изолируем именно текст)
    # больше не видит OOM соседа.
    facts = OOMKilledRule().evaluate({"pod": _TARGET_POD, "logs_summary": snap.text})
    assert len(facts) == 1
    assert facts[0].kind == FactKind.OOM_KILLED
    assert facts[0].observed is False


def test_target_workload_terminated_line_still_inlined(monkeypatch):
    """Пересозданный под того же workload-а (другой rs-hash) — свой сигнал."""
    recreated = "town-service-0000a-qqqqq"
    _install_fake_k8s(monkeypatch, _FakeCoreV1(
        pods=[_pod(recreated, terminated=_terminated("OOMKilled", 137))],
    ))
    snap = K8sFacts._collect_sync("prod-shared", pod=_TARGET_POD)

    assert f"Container terminated: {recreated}/app reason=OOMKilled" in snap.text
    facts = OOMKilledRule().evaluate({"pod": _TARGET_POD, "logs_summary": snap.text})
    assert facts[0].observed is True
    assert facts[0].confidence >= 0.9


def test_foreign_pod_logs_are_not_collected(monkeypatch):
    api = _install_fake_k8s(monkeypatch, _FakeCoreV1(
        pods=[_pod(_FOREIGN_POD, terminated=_terminated("Error", 1))],
        logs={_FOREIGN_POD: "fatal: container ran out of memory, exit code 137"},
    ))
    snap = K8sFacts._collect_sync("prod-shared", pod=_TARGET_POD)

    assert _FOREIGN_POD not in api.logs_requested
    assert "out of memory" not in snap.text
    assert "logs not collected" in snap.text
    facts = OOMKilledRule().evaluate({"pod": _TARGET_POD, "logs_summary": snap.text})
    assert facts[0].observed is False


def test_no_target_keeps_namespace_wide_blob(monkeypatch):
    """Без target скоупить не по чему — blob как раньше, деградирует правило."""
    _install_fake_k8s(monkeypatch, _FakeCoreV1(
        pods=[_pod(_FOREIGN_POD, terminated=_terminated("OOMKilled", 137))],
    ))
    snap = K8sFacts._collect_sync("prod-shared", pod=None)

    assert "reason=OOMKilled" in snap.text
    # Привязать текст не к чему → observed остаётся, но confidence в soft-зоне
    # fact_critic ([0.25, 0.5)): мягкий штраф вместо жёсткого anchor-а.
    facts = OOMKilledRule().evaluate({"logs_summary": snap.text})
    assert facts[0].observed is True
    assert 0.25 <= facts[0].confidence < 0.5
    assert facts[0].evidence["attribution"] == "unverified"


# ---------------------------------------------------------------------------
# Редакция pod-логов до попадания в LLM-контекст
# ---------------------------------------------------------------------------

def test_pod_logs_are_redacted_in_snapshot_text(monkeypatch):
    raw = (
        "AuthError for user yar.shulgin@gmail.com from 10.42.13.7\n"
        "Authorization: Bearer abcdefghij1234567890XYZ\n"
        "anthropic key sk-ant-api03-AAAAbbbbCCCCddddEEEE1234\n"
        "db password=hunter2\n"
    )
    _install_fake_k8s(monkeypatch, _FakeCoreV1(
        pods=[_pod(_TARGET_POD, terminated=_terminated("Error", 1))],
        logs={_TARGET_POD: raw},
    ))
    snap = K8sFacts._collect_sync("prod-shared", pod=_TARGET_POD)

    for leak in ("yar.shulgin@gmail.com", "10.42.13.7",
                 "abcdefghij1234567890XYZ", "sk-ant-api03", "hunter2"):
        assert leak not in snap.text, f"leaked: {leak!r}"
    assert "<email>" in snap.text
    assert "<ip>" in snap.text
    assert "<anthropic-key>" in snap.text
    # Логи не усекаются до 500 символов Discord-лимитом: диагностика цела.
    assert "AuthError" in snap.text
    assert "[truncated]" not in snap.text


def test_terminated_message_is_redacted(monkeypatch):
    _install_fake_k8s(monkeypatch, _FakeCoreV1(
        pods=[
            _pod(
                _TARGET_POD,
                terminated=_terminated(
                    "Error", 1,
                    "conn failed: postgres://svc:hunter2@10.0.0.5:5432 "
                    "token=xoxb-1234567890-abcdefghijkl",
                ),
            ),
        ],
    ))
    snap = K8sFacts._collect_sync("prod-shared", pod=_TARGET_POD)

    msg = snap.container_terminated[_TARGET_POD]["message"]
    assert "hunter2" not in msg
    assert "10.0.0.5" not in msg
    assert "xoxb-1234567890" not in msg
    assert "hunter2" not in snap.text


# ---------------------------------------------------------------------------
# LogCollector (context_builder → LLM) тоже редактирует
# ---------------------------------------------------------------------------

def test_log_collector_summary_is_redacted(monkeypatch):
    import app.context.logs as logs_mod

    raw = (
        "startup ok\n"
        "ERROR user admin@example.org token=xoxb-1234567890-abcdefghijkl "
        "db postgres://svc:hunter2@10.1.2.3:5432/town failed\n"
    )

    class _Api:
        def read_namespaced_pod_log(self, name, namespace, tail_lines=None):
            return raw

    monkeypatch.setattr(logs_mod, "client", SimpleNamespace(CoreV1Api=lambda: _Api()))
    out = logs_mod.LogCollector().get_summary("prod-shared", _TARGET_POD)

    for leak in ("admin@example.org", "xoxb-1234567890", "hunter2", "10.1.2.3"):
        assert leak not in out, f"leaked: {leak!r}"
    assert "<email>" in out
    assert "ERROR" in out


def test_log_collector_returns_tail_not_head(monkeypatch):
    """Отдаём хвост логов: диагностика — в последних строках, не в первых."""
    import app.context.logs as logs_mod

    raw = "head-marker\n" + ("filler line\n" * 200) + "tail-marker\n"

    class _Api:
        def read_namespaced_pod_log(self, name, namespace, tail_lines=None):
            return raw

    monkeypatch.setattr(logs_mod, "client", SimpleNamespace(CoreV1Api=lambda: _Api()))
    out = logs_mod.LogCollector().get_summary("prod-shared", _TARGET_POD)

    assert "tail-marker" in out
    assert "head-marker" not in out
    assert len(out) <= 500


# ---------------------------------------------------------------------------
# PodEventsRule: событие чужого объекта не даёт observed-anchor
# ---------------------------------------------------------------------------

def test_event_of_foreign_object_gives_no_observed_anchor():
    facts = PodEventsRule().evaluate({
        "pod": _TARGET_POD,
        "namespace": "prod-shared",
        "k8s_events": [
            {"reason": "OOMKilling", "message": "Memory cgroup out of memory",
             "count": 3, "object": _FOREIGN_POD},
        ],
    })
    assert facts, "факт-пометка про чужой объект должна остаться в store"
    assert all(f.observed is False for f in facts)
    oom = [f for f in facts if f.kind == FactKind.OOM_KILLED]
    assert oom and oom[0].evidence["attribution"] == "other_workload"
    assert oom[0].evidence["object"] == _FOREIGN_POD


def test_event_of_target_workload_keeps_full_confidence():
    facts = PodEventsRule().evaluate({
        "pod": _TARGET_POD,
        "k8s_events": [
            {"reason": "OOMKilling", "message": "oom", "count": 2,
             "object": "town-service-0000a-qqqqq"},
        ],
    })
    assert len(facts) == 1
    assert facts[0].observed is True
    assert facts[0].confidence == 0.95
    assert facts[0].subject == "town-service-0000a-qqqqq"


def test_event_scoped_by_service_label_when_pod_unknown():
    """У алерта нет pod, но есть service — скоупим по нему (KG-форма события)."""
    facts = PodEventsRule().evaluate({
        "service": "town-service",
        "k8s_events": [
            {"reason": "OOMKilling", "message": "oom", "count": 1,
             "pod_name": _TARGET_POD},
            {"reason": "FailedScheduling", "message": "no nodes", "count": 1,
             "pod_name": _FOREIGN_POD},
        ],
    })
    by_kind = {f.kind: f for f in facts}
    assert by_kind[FactKind.OOM_KILLED].observed is True
    assert by_kind[FactKind.OOM_KILLED].confidence == 0.95
    assert by_kind[FactKind.FAILED_SCHEDULING].observed is False


def test_unattributable_event_is_degraded_to_soft_zone():
    """Ни pod, ни service у алерта → событие ns-wide, не жёсткий anchor."""
    facts = PodEventsRule().evaluate({
        "namespace": "prod-shared",
        "k8s_events": [
            {"reason": "OOMKilling", "message": "oom", "count": 1,
             "object": _FOREIGN_POD},
        ],
    })
    assert len(facts) == 1
    f = facts[0]
    assert f.observed is True
    assert 0.25 <= f.confidence < 0.5
    assert f.evidence["attribution"] == "unverified"
    assert f.subject == "prod-shared"


def test_event_without_object_is_degraded_not_dropped():
    """Событие без involvedObject при известном target — привязку не проверить."""
    facts = PodEventsRule().evaluate({
        "pod": _TARGET_POD,
        "k8s_events": [{"reason": "Evicted", "message": "node pressure"}],
    })
    assert len(facts) == 1
    assert facts[0].kind == FactKind.RESOURCE_PRESSURE
    assert facts[0].observed is True
    assert 0.25 <= facts[0].confidence < 0.5


def test_scoped_event_wins_over_foreign_same_kind():
    """Свой OOM + чужой OOM → один observed-факт про свой под."""
    facts = PodEventsRule().evaluate({
        "pod": _TARGET_POD,
        "k8s_events": [
            {"reason": "OOMKilling", "message": "oom", "count": 9,
             "object": _FOREIGN_POD},
            {"reason": "OOMKilling", "message": "oom", "count": 1,
             "object": _TARGET_POD},
        ],
    })
    oom = [f for f in facts if f.kind == FactKind.OOM_KILLED]
    assert len(oom) == 1
    assert oom[0].observed is True
    assert oom[0].subject == _TARGET_POD


def test_empty_events_still_produce_no_facts():
    assert PodEventsRule().evaluate({"pod": _TARGET_POD, "k8s_events": []}) == []
