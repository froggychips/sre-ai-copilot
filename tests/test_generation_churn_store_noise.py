"""GenerationMismatch на пути /store: churn фонового писателя → noise.

Замер 24.09.2026: Rancher раз в минуту переписывает Deployment с публичным
ingress (generation 23 тыс. при revision 4), deployment-контроллер отстаёт,
KubeDeploymentGenerationMismatch мигает — 533 инцидента за сутки в squad-*,
ни один не помечен: health-gate живёт только на enrich-пути.

Критерий (classify_generation_churn): наката нет (Progressing=
NewReplicaSetAvailable давно, все реплики обновлены и готовы) И spec последним
писал фоновый менеджер. Любая неоднозначность — не шум.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from unittest.mock import patch

import pytest

from app.models.incident import AlertManagerAlert
from app.services.alert_enrichment import classify_generation_churn

NOW = datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc)


def _state(**over: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "desired": 1, "ready": 1, "updated": 1, "unavailable": 0,
        "progressing_reason": "NewReplicaSetAvailable",
        "progressing_updated_at": NOW - timedelta(days=13),
        "revision": "4",
        "writers": [
            {"manager": "helm", "operation": "Update", "subresource": "", "time": NOW - timedelta(days=13)},
            {"manager": "rancher", "operation": "Update", "subresource": "", "time": NOW - timedelta(seconds=40)},
            {"manager": "kube-controller-manager", "operation": "Update", "subresource": "status",
             "time": NOW - timedelta(seconds=5)},
        ],
    }
    base.update(over)
    return base


def _churn(state) -> bool:
    return classify_generation_churn(state, background_managers=["rancher"], quiet_minutes=30, now=NOW)


def test_rancher_churn_on_settled_deployment_is_noise():
    assert _churn(_state()) is True


@pytest.mark.parametrize("over", [
    {"progressing_reason": "ReplicaSetUpdated"},            # накат идёт
    {"progressing_reason": "ProgressDeadlineExceeded"},     # накат упал
    {"progressing_updated_at": NOW - timedelta(minutes=5)},  # накат только что
    {"ready": 0},
    {"updated": 0},                                          # новый RS не раскатан
    {"unavailable": 1},
    {"desired": 0, "ready": 0, "updated": 0},
    {"progressing_updated_at": None},
])
def test_real_or_unclear_rollout_stays_incident(over):
    assert _churn(_state(**over)) is False


def test_human_or_ci_last_spec_writer_is_not_noise():
    st = _state()
    st["writers"].append({"manager": "helm", "operation": "Update", "subresource": "",
                          "time": NOW - timedelta(seconds=10)})
    assert _churn(st) is False


def test_status_writer_does_not_count_as_spec_writer():
    st = _state()
    st["writers"] = [w for w in st["writers"] if w["manager"] != "rancher"]
    # последний не-status писатель — helm → не шум, хотя status писал контроллер позже
    assert _churn(st) is False


@pytest.mark.parametrize("state", [None, {}, _state(writers=[])])
def test_no_data_is_not_noise(state):
    assert _churn(state) is False


def test_empty_manager_list_disables():
    assert classify_generation_churn(_state(), background_managers=[], quiet_minutes=30, now=NOW) is False


def test_naive_datetimes_are_treated_as_utc():
    st = _state(progressing_updated_at=(NOW - timedelta(days=1)).replace(tzinfo=None))
    for w in st["writers"]:
        w["time"] = w["time"].replace(tzinfo=None)
    assert _churn(st) is True


# --- store-путь: какие алерты уходят на проверку ------------------------------

def _alert(alertname="KubeDeploymentGenerationMismatch", status="firing", **labels) -> AlertManagerAlert:
    lab = {"alertname": alertname, "namespace": "squad-18-shared", "deployment": "analytics-service",
           "severity": "warning", **labels}
    return AlertManagerAlert(status=status, labels=lab, annotations={},
                             startsAt="2026-09-24T09:00:00Z", fingerprint="fp-" + lab["deployment"])


def test_store_targets_only_classified_firing_generation_mismatch(monkeypatch):
    from app.api import webhooks
    from app.config import settings

    monkeypatch.setattr(settings, "GEN_MISMATCH_STORE_NOISE_ENABLED", True)
    states = {
        ("squad-18-shared", "analytics-service"): _state(),
        ("squad-18-shared", "town-service"): _state(progressing_reason="ProgressDeadlineExceeded"),
    }
    seen = []

    def fake_fetch(ns, name, timeout_sec=3.0):
        seen.append((ns, name))
        if name == "boom":
            raise RuntimeError("api down")
        return states.get((ns, name))

    alerts = [
        _alert(),
        _alert(deployment="town-service"),
        _alert(deployment="boom"),
        _alert(status="resolved", deployment="resolved-svc"),
        _alert(alertname="KubePodCrashLooping", deployment="other"),
    ]
    # Прогресс в снимках — 13 дней назад: исход от реального «сейчас» не зависит.
    with patch("app.context.deployments.fetch_deployment_rollout_state", side_effect=fake_fetch):
        got = asyncio.run(webhooks._generation_churn_targets(alerts))
    assert got == {("squad-18-shared", "analytics-service")}
    assert sorted(seen) == [("squad-18-shared", "analytics-service"), ("squad-18-shared", "boom"),
                            ("squad-18-shared", "town-service")]


def test_store_targets_kill_switch(monkeypatch):
    from app.api import webhooks
    from app.config import settings

    monkeypatch.setattr(settings, "GEN_MISMATCH_STORE_NOISE_ENABLED", False)
    with patch("app.context.deployments.fetch_deployment_rollout_state") as f:
        assert asyncio.run(webhooks._generation_churn_targets([_alert()])) == set()
        f.assert_not_called()
