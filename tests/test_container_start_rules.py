"""ImagePullRule и ContainerConfigRule: «контейнер не стартовал».

Формулировки сообщений — дословно как у kubelet/containerd, имена
обезличены (alpha/bravo, registry.example.org).
"""
from __future__ import annotations

from types import SimpleNamespace

from app.diagnostics import default_engine
from app.diagnostics.facts import FactKind
from app.diagnostics.rules import (ContainerConfigRule, CrashLoopBackOffRule,
                                   DEFAULT_RULES, ImagePullRule, PodEventsRule)
from app.services.alert_enrichment import _fact_to_short_text

_IMAGE = "registry.example.org/alpha/town-service:1.4.2"
_POD = "town-service-7d9f8c6b5-x2k4p"


def _ev(reason, message, obj=_POD, count=3):
    return {"type": "Warning", "reason": reason, "message": message,
            "object": obj, "count": count}


def _pull_not_found():
    return _ev("Failed", f'Failed to pull image "{_IMAGE}": rpc error: code = NotFound '
                         f'desc = failed to pull and unpack image "{_IMAGE}": failed to '
                         f'resolve reference "{_IMAGE}": {_IMAGE}: not found')


def _pull_backoff():
    return _ev("BackOff", f'Back-off pulling image "{_IMAGE}"', count=41)


def _one(rule, ctx):
    facts = rule.run(ctx)
    assert len(facts) == 1
    return facts[0]


# ── ImagePullRule ───────────────────────────────────────────────────────

def test_image_pull_found_from_scoped_event_with_cause_and_image():
    f = _one(ImagePullRule(), {"pod": _POD, "k8s_events": [_pull_not_found()]})
    assert f.kind == FactKind.IMAGE_PULL and f.observed
    assert f.confidence == 0.95
    assert f.evidence["cause"] == "not_found"
    assert f.evidence["image"] == _IMAGE


def test_image_pull_backoff_event_counts_as_image_pull():
    f = _one(ImagePullRule(), {"pod": _POD, "k8s_events": [_pull_backoff()]})
    assert f.observed and f.evidence["count"] == 41


def test_image_pull_distinguishes_auth_and_network():
    auth = _ev("Failed", f'Failed to pull image "{_IMAGE}": rpc error: code = Unknown '
                         'desc = failed to authorize: 401 Unauthorized')
    net = _ev("Failed", f'Failed to pull image "{_IMAGE}": rpc error: code = Unknown desc = '
                        'failed to do request: Head "https://registry.example.org/v2/": '
                        'dial tcp: lookup registry.example.org: no such host')
    assert _one(ImagePullRule(), {"pod": _POD, "k8s_events": [auth]}).evidence["cause"] == "auth"
    assert _one(ImagePullRule(), {"pod": _POD, "k8s_events": [net]}).evidence["cause"] == "network"


def test_image_pull_from_waiting_state_text():
    ctx = {"pod": _POD, "k8s_events": [],
           "logs_summary": f"Container waiting: {_POD}/app reason=ImagePullBackOff — "
                           f'Back-off pulling image "{_IMAGE}"'}
    f = _one(ImagePullRule(), ctx)
    assert f.observed and f.evidence["source"] == "k8s_text"
    assert f.evidence["image"] == _IMAGE


def test_image_pull_foreign_workload_is_absent_not_found():
    ev = _pull_not_found() | {"object": "bravo-worker-5c8d7-abcde"}
    f = _one(ImagePullRule(), {"pod": _POD, "k8s_events": [ev]})
    assert f.is_absent and f.evidence["attribution"] == "foreign"


def test_image_pull_unverified_attribution_halves_confidence():
    f = _one(ImagePullRule(), {"k8s_events": [_pull_not_found()], "namespace": "squad-alpha"})
    assert f.observed and f.confidence == 0.475
    assert f.evidence["attribution"] == "unverified"


def test_image_pull_absent_when_sources_healthy():
    f = _one(ImagePullRule(), {"pod": _POD, "k8s_events": [
        _ev("BackOff", "Back-off restarting failed container app in pod x")]})
    assert f.is_absent


def test_image_pull_absent_becomes_unknown_when_events_source_failed():
    f = _one(ImagePullRule(), {"pod": _POD, "k8s_events": [],
                               "source_status": {"k8s_events": "failed: ApiException"}})
    assert f.is_unknown


def test_pull_backoff_is_not_crashloop_any_more():
    """«Back-off pulling image» — процесс не стартовал, это не restart-цикл."""
    ctx = {"pod": _POD, "k8s_events": [_pull_backoff()]}
    assert _one(CrashLoopBackOffRule(), ctx).is_absent
    assert not any(f.kind == FactKind.CRASHLOOP and f.observed
                   for f in PodEventsRule().run(ctx))


def test_any_pull_signal_is_not_crashloop():
    """reason ImagePullBackOff содержит «backoff»; BackOff c «Failed to pull» — тоже pull."""
    for ev in (_ev("ImagePullBackOff", f'Back-off pulling image "{_IMAGE}"'),
               _ev("BackOff", f'Failed to pull image "{_IMAGE}": not found'),
               _ev("ErrImagePull", "rpc error")):
        ctx = {"pod": _POD, "k8s_events": [ev]}
        assert not any(f.kind == FactKind.CRASHLOOP and f.observed
                       for f in PodEventsRule().run(ctx)), ev["reason"]
        assert _one(CrashLoopBackOffRule(), ctx).is_absent, ev["reason"]


def test_detailed_failed_event_wins_over_frequent_backoff():
    f = _one(ImagePullRule(), {"pod": _POD, "k8s_events": [
        _pull_not_found() | {"count": 2}, _pull_backoff() | {"count": 400}]})
    assert f.evidence["cause"] == "not_found"


def test_app_log_secret_not_found_is_not_container_config():
    """Живой процесс пишет в лог ошибку клиента k8s — это не CreateContainerConfigError."""
    ctx = {"pod": _POD, "k8s_events": [],
           "logs_summary": 'ERROR reconcile: secrets "tls-cert" not found, retrying'}
    assert _one(ContainerConfigRule(), ctx).is_absent


def test_restart_backoff_still_crashloop():
    ctx = {"pod": _POD, "k8s_events": [
        _ev("BackOff", f"Back-off restarting failed container app in pod {_POD}")]}
    assert _one(CrashLoopBackOffRule(), ctx).observed


# ── ContainerConfigRule ─────────────────────────────────────────────────

def _missing_key(key, obj="squad-alpha/town-secrets", kind="Secret"):
    return _ev("Failed", f"Error: couldn't find key {key} in {kind} {obj}")


def test_container_config_found_with_keys_and_object():
    ctx = {"pod": _POD, "k8s_events": [
        _missing_key("SMTP_PASSWORD"), _missing_key("FEED_WEBHOOK_URL"),
        _ev("Failed", "Error: CreateContainerConfigError")]}
    f = _one(ContainerConfigRule(), ctx)
    assert f.kind == FactKind.CONTAINER_CONFIG and f.observed
    assert f.evidence["object_kind"] == "secret"
    assert f.evidence["object_name"] == "town-secrets"
    assert f.evidence["missing_keys"] == ["SMTP_PASSWORD", "FEED_WEBHOOK_URL"]


def test_container_config_missing_configmap_object():
    ctx = {"pod": _POD, "k8s_events": [
        _ev("Failed", 'Error: configmap "town-config" not found')]}
    f = _one(ContainerConfigRule(), ctx)
    assert f.observed and f.evidence["missing_object"] is True
    assert f.evidence["object_kind"] == "configmap"
    assert f.evidence["object_name"] == "town-config"


def test_container_config_from_waiting_state_text_keeps_key_case():
    ctx = {"pod": _POD, "k8s_events": [],
           "logs_summary": f"Container waiting: {_POD}/app reason=CreateContainerConfigError"
                           " — couldn't find key BOT_FAST_FORWARD in Secret squad-alpha/bot-env"}
    f = _one(ContainerConfigRule(), ctx)
    assert f.observed and f.evidence["missing_keys"] == ["BOT_FAST_FORWARD"]
    assert f.evidence["object_name"] == "bot-env"


def test_container_config_foreign_workload_is_absent():
    ctx = {"pod": _POD, "k8s_events": [_missing_key("X", obj="ns/other") | {
        "object": "bravo-api-6f5d4-zzzzz"}]}
    assert _one(ContainerConfigRule(), ctx).is_absent


def test_container_config_absent_and_unknown():
    assert _one(ContainerConfigRule(), {"pod": _POD, "k8s_events": []}).is_absent
    f = _one(ContainerConfigRule(), {"pod": _POD, "k8s_events": [],
                                     "source_status": {"logs_summary": "failed: timeout"}})
    assert f.is_unknown


def test_container_config_never_reads_values():
    """В evidence только имена ключей: значений kubelet не пишет, правило их не ищет."""
    ctx = {"pod": _POD, "k8s_events": [_missing_key("DB_PASSWORD")]}
    f = _one(ContainerConfigRule(), ctx)
    assert set(f.evidence) >= {"missing_keys", "object_name"}
    assert "value" not in " ".join(f.evidence)


# ── реестр, движок, эмбед, снапшот ──────────────────────────────────────

def test_rules_registered_and_reach_prompt_context():
    names = {r.name for r in DEFAULT_RULES}
    assert {"ImagePullRule", "ContainerConfigRule"} <= names
    store = default_engine.run({"pod": _POD, "k8s_events": [
        _pull_not_found(), _missing_key("SMTP_PASSWORD")]})
    prompt = store.to_prompt_context()
    assert "✓ image_pull" in prompt and "✓ container_config" in prompt


def test_short_text_for_embed():
    f = _one(ImagePullRule(), {"pod": _POD, "k8s_events": [_pull_not_found()]})
    assert "тега/манифеста нет в registry" in _fact_to_short_text(f)
    f = _one(ContainerConfigRule(), {"pod": _POD, "k8s_events": [_missing_key("SMTP_PASSWORD")]})
    assert "SMTP_PASSWORD" in _fact_to_short_text(f)


def _pod(name, phase, waiting=None):
    cs = SimpleNamespace(
        name="app", restart_count=0,
        last_state=SimpleNamespace(terminated=None),
        state=SimpleNamespace(terminated=None, waiting=waiting),
    )
    return SimpleNamespace(metadata=SimpleNamespace(name=name),
                           status=SimpleNamespace(phase=phase, container_statuses=[cs]),
                           spec=SimpleNamespace(containers=[], node_name="dev-1"))


class _FakeCoreV1:
    def __init__(self, pods):
        self._pods = pods

    def list_namespaced_pod(self, namespace):
        return SimpleNamespace(items=self._pods)

    def read_namespaced_pod_log(self, name, namespace, tail_lines=None, previous=False):
        raise RuntimeError("no logs in fixture")

    def list_namespaced_event(self, namespace, field_selector=None):
        return SimpleNamespace(items=[])


def test_snapshot_text_carries_waiting_state_scoped_to_target(monkeypatch):
    import app.context.k8s_facts as mod

    target = _pod(_POD, "Pending", SimpleNamespace(
        reason="ImagePullBackOff", message=f'Back-off pulling image "{_IMAGE}"'))
    foreign = _pod("bravo-api-6f5d4-zzzzz", "Pending", SimpleNamespace(
        reason="CreateContainerConfigError", message="couldn't find key X in Secret ns/s"))
    starting = _pod("town-service-7d9f8c6b5-new01", "Pending",
                    SimpleNamespace(reason="ContainerCreating", message=""))
    api = _FakeCoreV1([target, foreign, starting])
    monkeypatch.setattr(mod, "k8s_config", SimpleNamespace(
        load_incluster_config=lambda: None, load_kube_config=lambda: None))
    monkeypatch.setattr(mod, "client", SimpleNamespace(CoreV1Api=lambda: api))

    snap = mod.K8sFacts._collect_sync("squad-alpha", _POD)
    assert f"Container waiting: {_POD}/app reason=ImagePullBackOff" in snap.text
    assert "CreateContainerConfigError" not in snap.text   # чужой workload — без reason
    assert "ContainerCreating" not in snap.text            # штатный старт — не сигнал
    # и правило ловит это по тексту снапшота
    f = _one(ImagePullRule(), {"pod": _POD, "k8s_events": [], "logs_summary": snap.text})
    assert f.observed and f.evidence["image"] == _IMAGE


def test_image_pull_backoff_pod_yields_image_pull_not_crashloop_in_engine():
    """Под в ImagePullBackOff: image_pull ✓, crashloop не ✓ ни одним правилом."""
    store = default_engine.run({"pod": _POD, "k8s_events": [
        _ev("Failed", f'Failed to pull image "{_IMAGE}": {_IMAGE}: not found'),
        _ev("Failed", "Error: ErrImagePull"),
        _pull_backoff(),
    ]})
    assert store.has_observed(FactKind.IMAGE_PULL)
    assert not store.has_observed(FactKind.CRASHLOOP)
    assert store.conflicts() == []


def test_image_pull_and_crashloop_same_subject_is_conflict_with_cap():
    """Если оба всё же ✓ про один под (текстовый сигнал рестарта) — конфликт."""
    store = default_engine.run({"pod": _POD, "k8s_events": [_pull_backoff()],
                                "description": "CrashLoopBackOff"})
    assert store.has_observed(FactKind.IMAGE_PULL)
    assert store.has_observed(FactKind.CRASHLOOP)
    pairs = {frozenset({a.kind, b.kind}) for a, b in store.conflicts()}
    assert frozenset({FactKind.IMAGE_PULL, FactKind.CRASHLOOP}) in pairs
    assert all(f.confidence <= 0.60 for f in store.facts
               if f.kind in (FactKind.IMAGE_PULL, FactKind.CRASHLOOP) and f.observed)
