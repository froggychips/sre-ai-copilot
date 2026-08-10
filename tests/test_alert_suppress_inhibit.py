"""Unit-тесты A1 (inhibit-aware) + A3 (allowlist suppress).

A1 покрывает:
  - `_inhibition_state` корректно парсит status_extra (dict) и labels fallback.
  - `EnrichedContext.inhibition_state` заполняется из incident.status_extra.
  - AlertManagerAlert модель принимает status как объект AM API v2.

A3 покрывает:
  - `_suppress_names` объединяет дефолты + env-extra.
  - `_alert_in_allowlist` substring-match для KubeAPIServerSloMaster.
  - `_filter_suppressed` пропускает обычные, режет noise.
  - `_is_self_noise` ловит severity=info + service=monitoring.

Postgres не нужен — тесты только на pure-Python функции.
"""
from __future__ import annotations

from app.api.webhooks import (_alert_in_allowlist, _filter_suppressed,
                              _is_self_noise, _suppress_names)
from app.models.incident import AlertManagerAlert, Incident
from app.services.alert_enrichment import _inhibition_state


# ── A1.1: AlertManagerAlert модель ────────────────────────────────────────


def _mk_alert(status=None, labels=None, fingerprint="fp-1"):
    return AlertManagerAlert(
        status=status if status is not None else "firing",
        labels=labels or {"alertname": "X"},
        startsAt="2026-05-15T12:00:00Z",
        fingerprint=fingerprint,
    )


def test_am_alert_accepts_legacy_string_status():
    """Backward compat: webhook v4 шлёт `status: "firing"` строкой."""
    a = _mk_alert(status="firing")
    assert a.status == "firing"
    assert a.status_extra is None


def test_am_alert_accepts_object_status_v2():
    """AM API v2 шлёт `status` объектом — нормализуем к firing+extra."""
    a = _mk_alert(status={
        "state": "suppressed",
        "silencedBy": ["sil-abcd1234"],
        "inhibitedBy": [],
    })
    assert a.status == "firing"
    assert a.status_extra is not None
    assert a.status_extra["state"] == "suppressed"
    assert a.status_extra["silencedBy"] == ["sil-abcd1234"]


def test_am_alert_resolved_object_status():
    """state=resolved → status=resolved."""
    a = _mk_alert(status={"state": "resolved"})
    assert a.status == "resolved"
    assert a.status_extra == {"state": "resolved"}


def test_am_alert_labels_fallback_for_silenced_by():
    """Самописные шлюзы кладут `silenced_by` прямо в labels."""
    a = _mk_alert(
        status="firing",
        labels={"alertname": "X", "silenced_by": "sil-12345abc"},
    )
    assert a.status_extra is not None
    assert a.status_extra["state"] == "suppressed"
    assert "sil-12345abc" in a.status_extra["silencedBy"]


# ── A1.1b: валидатор не мутирует входной payload ──────────────────────────


def test_object_status_validation_does_not_mutate_input_payload():
    """`mode="before"`-валидатор не правит dict вызывающего.

    Сюда прилетает ровно тот объект, что держит вызывающий — распарсенное тело
    AM-вебхука. Раньше валидатор перезаписывал `data["status"]` на месте:
    сохранённый «сырой» алерт врал про то, что реально пришло от AM, а
    повторная валидация того же dict-а уже не видела объект-status.
    """
    original_status = {
        "state": "suppressed",
        "silencedBy": ["sil-abcd1234"],
        "inhibitedBy": [],
    }
    payload = {
        "status": original_status,
        "labels": {"alertname": "X"},
        "startsAt": "2026-05-15T12:00:00Z",
        "fingerprint": "fp-1",
    }
    snapshot = {"status": dict(original_status), "labels": {"alertname": "X"},
                "startsAt": "2026-05-15T12:00:00Z", "fingerprint": "fp-1"}

    alert = AlertManagerAlert.model_validate(payload)

    assert alert.status == "firing"
    assert alert.status_extra == original_status
    assert payload == snapshot, "валидатор изменил входной dict"
    assert payload["status"] is original_status
    assert "status_extra" not in payload

    # Повторная валидация того же payload даёт тот же результат.
    again = AlertManagerAlert.model_validate(payload)
    assert (again.status, again.status_extra) == (alert.status, alert.status_extra)


def test_labels_fallback_does_not_mutate_input_payload():
    """labels-fallback тоже не дописывает `status_extra` в чужой dict."""
    payload = {
        "status": "firing",
        "labels": {"alertname": "X", "inhibited_by": "inh-1"},
        "startsAt": "2026-05-15T12:00:00Z",
        "fingerprint": "fp-2",
    }
    snapshot = {"status": "firing",
                "labels": {"alertname": "X", "inhibited_by": "inh-1"},
                "startsAt": "2026-05-15T12:00:00Z", "fingerprint": "fp-2"}

    alert = AlertManagerAlert.model_validate(payload)

    assert alert.status_extra is not None
    assert alert.status_extra["inhibitedBy"] == ["inh-1"]
    assert payload == snapshot, "валидатор изменил входной dict"


def test_plain_string_status_returns_payload_untouched():
    """Обычный firing-алерт без suppress-признаков — payload не копируется зря."""
    payload = {
        "status": "firing",
        "labels": {"alertname": "X"},
        "startsAt": "2026-05-15T12:00:00Z",
        "fingerprint": "fp-3",
    }
    alert = AlertManagerAlert.model_validate(payload)
    assert alert.status_extra is None
    assert payload == {
        "status": "firing",
        "labels": {"alertname": "X"},
        "startsAt": "2026-05-15T12:00:00Z",
        "fingerprint": "fp-3",
    }


# ── A1.2: _inhibition_state ───────────────────────────────────────────────


def _inc(status_extra=None, labels=None, severity="warning"):
    return Incident(
        incident_id="i-1",
        severity=severity,
        status="firing",
        summary="",
        namespace="ns",
        labels=labels or {},
        annotations={},
        starts_at="2026-05-15T12:00:00Z",
        status_extra=status_extra,
    )


def test_inhibition_state_returns_none_when_no_extra():
    assert _inhibition_state(_inc()) is None


def test_inhibition_state_returns_none_when_state_active():
    assert _inhibition_state(_inc(status_extra={"state": "active"})) is None


def test_inhibition_state_silenced_only():
    s = _inhibition_state(_inc(status_extra={
        "state": "suppressed", "silencedBy": ["sil-abcd1234"],
    }))
    assert s is not None
    assert "silenced" in s
    assert "sil-abcd" in s


def test_inhibition_state_inhibited_by_alertname():
    s = _inhibition_state(_inc(status_extra={
        "state": "suppressed", "inhibitedBy": ["KubePodCrashLooping"],
    }))
    assert s is not None
    assert "inhibited" in s
    assert "KubePodCrashLooping" in s


def test_inhibition_state_both_silenced_and_inhibited():
    s = _inhibition_state(_inc(status_extra={
        "state": "suppressed",
        "silencedBy": ["sil-1"],
        "inhibitedBy": ["fp-deadbeefcafe"],
    }))
    assert s is not None
    assert "silenced" in s and "inhibited" in s


def test_inhibition_state_labels_fallback():
    """Через labels.silenced_by — без AM v2 объекта."""
    inc = _inc(labels={"silenced_by": "sil-x"})
    s = _inhibition_state(inc)
    assert s is not None
    assert "sil-x" in s


def test_inhibition_state_raw_dict_input():
    """Функция принимает не только Incident, но и raw AM payload."""
    raw = {
        "status": {"state": "suppressed", "inhibitedBy": ["X"]},
        "labels": {"alertname": "Y"},
    }
    s = _inhibition_state(raw)
    assert s is not None
    assert "inhibited" in s


# ── A3.1: _suppress_names + _alert_in_allowlist ───────────────────────────


def test_suppress_names_returns_defaults():
    names = _suppress_names()
    assert "Watchdog" in names
    assert "InfoInhibitor" in names
    assert "KubeAPIServerSlo" in names


def test_suppress_names_env_extra_extends_defaults(monkeypatch):
    """ALERT_SUPPRESS_NAMES_EXTRA расширяет список через CSV."""
    from app.config import settings
    monkeypatch.setattr(settings, "ALERT_SUPPRESS_NAMES_EXTRA",
                        "CustomNoiseAlert,AnotherNoise")
    names = _suppress_names()
    assert "Watchdog" in names           # default остался
    assert "CustomNoiseAlert" in names   # env-extra добавлен
    assert "AnotherNoise" in names


def test_alert_in_allowlist_exact_match():
    assert _alert_in_allowlist("Watchdog", ["Watchdog", "Foo"]) == "Watchdog"


def test_alert_in_allowlist_substring_match():
    """Prefix `KubeAPIServerSlo` ловит и `Master`, и `Node` варианты."""
    assert _alert_in_allowlist("KubeAPIServerSloMaster",
                                ["KubeAPIServerSlo"]) == "KubeAPIServerSlo"


def test_alert_in_allowlist_no_match():
    assert _alert_in_allowlist("RealAlert", ["Watchdog", "Foo"]) is None


def test_alert_in_allowlist_empty_alertname():
    assert _alert_in_allowlist("", ["Watchdog"]) is None


# ── A3.2: _is_self_noise ──────────────────────────────────────────────────


def test_is_self_noise_severity_info_plus_monitoring():
    a = _mk_alert(labels={
        "alertname": "Some", "severity": "info", "service": "monitoring",
    })
    assert _is_self_noise(a) is True


def test_is_self_noise_warning_not_noise():
    a = _mk_alert(labels={
        "alertname": "Some", "severity": "warning", "service": "monitoring",
    })
    assert _is_self_noise(a) is False


def test_is_self_noise_info_outside_monitoring():
    a = _mk_alert(labels={
        "alertname": "Some", "severity": "info", "service": "billing",
    })
    assert _is_self_noise(a) is False


# ── A3.3: _filter_suppressed (integration of all filters) ─────────────────


def test_filter_suppressed_passes_regular_alerts():
    alerts = [
        _mk_alert(labels={"alertname": "KubePodCrashLooping", "severity": "warning"}),
        _mk_alert(labels={"alertname": "KubeDeploymentReplicasMismatch", "severity": "warning"}),
    ]
    passed, suppressed = _filter_suppressed(alerts)
    assert len(passed) == 2
    assert suppressed == 0


def test_filter_suppressed_blocks_allowlist():
    alerts = [
        _mk_alert(labels={"alertname": "Watchdog", "severity": "none"}, fingerprint="w1"),
        _mk_alert(labels={"alertname": "InfoInhibitor", "severity": "info"}, fingerprint="i1"),
        _mk_alert(labels={"alertname": "RealAlert", "severity": "warning"}, fingerprint="r1"),
    ]
    passed, suppressed = _filter_suppressed(alerts)
    assert suppressed == 2
    assert len(passed) == 1
    assert passed[0].labels["alertname"] == "RealAlert"


def test_filter_suppressed_blocks_kube_api_server_slo_variants():
    """Substring match: `KubeAPIServerSloMaster` режется паттерном `KubeAPIServerSlo`."""
    alerts = [
        _mk_alert(labels={"alertname": "KubeAPIServerSloMaster", "severity": "warning"}, fingerprint="a"),
        _mk_alert(labels={"alertname": "KubeAPIServerSloNode", "severity": "warning"}, fingerprint="b"),
    ]
    passed, suppressed = _filter_suppressed(alerts)
    assert suppressed == 2
    assert passed == []


def test_filter_suppressed_blocks_self_noise():
    """severity=info + service=monitoring → self-noise reason."""
    alerts = [
        _mk_alert(labels={
            "alertname": "SomeProbeFailed", "severity": "info",
            "service": "monitoring",
        }),
    ]
    passed, suppressed = _filter_suppressed(alerts)
    assert suppressed == 1
    assert passed == []


def test_filter_suppressed_env_override_extends(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "ALERT_SUPPRESS_NAMES_EXTRA",
                        "CustomBuildSpam")
    alerts = [
        _mk_alert(labels={"alertname": "CustomBuildSpam", "severity": "warning"}, fingerprint="z"),
        _mk_alert(labels={"alertname": "GoodAlert", "severity": "warning"}, fingerprint="g"),
    ]
    passed, suppressed = _filter_suppressed(alerts)
    assert suppressed == 1
    assert len(passed) == 1
    assert passed[0].labels["alertname"] == "GoodAlert"
