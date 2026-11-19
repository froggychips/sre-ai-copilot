"""Дрейф манифестов, который видно без кластера.

`scripts/manifest_drift.py` сверяет git с живым кластером, но в CI кластера
нет. Здесь — та часть, которая проверяется по одному репозиторию: что деплой
(`deploy.sh`) действительно доносит до кластера всё, что обещают манифесты.

Каждая проверка — след находки сверки 18.11.2026:

* CronJob-ы `squad-dashboard` и `ci-deadman` несли `IMAGE_PLACEHOLDER`, но
  deploy.sh их не применял: живые объекты стояли на образах 1.0.27 и
  1.0.0-rc.19 при приложении 1.0.35, а сырой `kubectl apply` записывал
  плейсхолдер в CronJob как есть — тик падал в InvalidImageName молча.
* В публичном манифесте лежал адрес внешней базы.
* RoleBinding указывал на ServiceAccount в namespace `default`, которого нет.
* postgres и redis стояли на плавающих тегах.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
K8S = ROOT / "k8s"
IMAGE_REPO = "registry.example/sre-ai-copilot"
IMAGE_TAG = "0.0.0-test"
DOC_CIDR = "203.0.113.10/32"  # RFC 5737

_PLACEHOLDER = re.compile(r"[A-Z_]+_PLACEHOLDER|IMAGE_PLACEHOLDER")


def _render(**extra_env: str) -> str:
    env = dict(os.environ, IMAGE_REPO=IMAGE_REPO, IMAGE_TAG=IMAGE_TAG, STATICS_DB_CIDR=DOC_CIDR)
    env.update(extra_env)
    res = subprocess.run(
        ["bash", str(ROOT / "deploy.sh"), "--render"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout


def _docs(text: str) -> list:
    return [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]


def _pod_spec(doc: dict) -> dict | None:
    kind = doc.get("kind")
    if kind in {"Deployment", "StatefulSet", "Job"}:
        return doc["spec"]["template"]["spec"]
    if kind == "CronJob":
        return doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    return None


def _code_lines(text: str) -> list:
    """Строки без комментариев: плейсхолдер в комментарии — не дефект."""
    return [line.split(" #", 1)[0] for line in text.splitlines()
            if not line.lstrip().startswith("#")]


# ── рендер деплоя ────────────────────────────────────────────────────────────


def test_render_needs_no_cluster_and_leaves_no_placeholders() -> None:
    rendered = _render()
    leftovers = [line for line in _code_lines(rendered) if _PLACEHOLDER.search(line)]
    assert not leftovers, f"после рендера остались плейсхолдеры: {leftovers[:5]}"
    assert len(_docs(rendered)) > 15


def test_every_app_container_gets_the_release_image() -> None:
    """Все контейнеры приложения — на одном образе, включая CronJob-ы и Job миграций."""
    seen = {}
    for doc in _docs(_render()):
        spec = _pod_spec(doc)
        if spec is None:
            continue
        for c in spec["containers"]:
            seen[f"{doc['kind']}/{doc['metadata']['name']}"] = c["image"]
    assert set(seen.values()) == {f"{IMAGE_REPO}:{IMAGE_TAG}"}, seen
    assert {"CronJob/squad-dashboard", "CronJob/ci-deadman", "Job/sre-ai-migrate",
            "Deployment/copilot-executor"} <= set(seen)


def test_every_templated_manifest_is_applied_by_deploy() -> None:
    """Манифест с плейсхолдером, который deploy.sh не применяет, — это объект,
    который выкатка не обновляет (так отстали CronJob-ы)."""
    deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
    listed = re.search(r'DEPLOY_MANIFESTS="(.*?)"', deploy, re.S).group(1)
    listed = listed.replace("${SETTINGS_MANIFEST}", "k8s/base/settings.yaml").split()
    templated = {
        str(p.relative_to(ROOT)) for p in K8S.rglob("*.yaml")
        if any(_PLACEHOLDER.search(line) for line in _code_lines(p.read_text(encoding="utf-8")))
    }
    assert templated, "плейсхолдеры не находятся — проверка стала бы вечно зелёной"
    assert templated <= set(listed), f"не применяются деплоем: {sorted(templated - set(listed))}"
    for path in listed:
        assert (ROOT / path).is_file(), path
        # Каждый файл из списка рендера действительно применяется шагом деплоя.
        assert re.search(rf'(apply_with_image|apply -f|apply_cronjob \S+) "?{re.escape(path)}', deploy) \
            or path == "k8s/base/settings.yaml", path


@pytest.mark.parametrize("name", ["squad-dashboard", "ci-deadman"])
def test_raw_apply_of_cronjob_is_rejected_not_silently_broken(name: str) -> None:
    """`image: ""` API-сервер отклоняет («Required value»). Невалидный образ он
    принимал, и CronJob молча переставал создавать поды."""
    (cj,) = _docs((K8S / f"{name}.yaml").read_text(encoding="utf-8"))
    (container,) = _pod_spec(cj)["containers"]
    assert container["image"] == ""
    raw = (K8S / f"{name}.yaml").read_text(encoding="utf-8")
    assert 'image: ""  # IMAGE_PLACEHOLDER' in raw, "маркер, по которому deploy.sh подставляет образ"


def test_deploy_refreshes_cronjob_scripts_with_the_image() -> None:
    """Скрипт CronJob-а лежит в ConfigMap: без его обновления «свежий образ»
    ничего не значит (ci-deadman-script отставал от scripts/ci_deadman.py)."""
    deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
    assert "apply_cronjob squad-dashboard k8s/squad-dashboard.yaml scripts/squad_dashboard.py" in deploy
    assert "apply_cronjob ci-deadman k8s/ci-deadman.yaml scripts/ci_deadman.py" in deploy
    for cj, script in (("squad-dashboard", "squad_dashboard.py"), ("ci-deadman", "ci_deadman.py")):
        (doc,) = _docs((K8S / f"{cj}.yaml").read_text(encoding="utf-8"))
        spec = _pod_spec(doc)
        assert spec["containers"][0]["command"] == ["python3", f"/scripts/{script}"]
        assert {"name": "script", "configMap": {"name": f"{cj}-script"}} in spec["volumes"]
        assert (ROOT / "scripts" / script).is_file()


# ── значения площадки ────────────────────────────────────────────────────────


def _statics_nodeport_rules(rendered: str) -> list:
    (policy,) = [d for d in _docs(rendered) if d["kind"] == "NetworkPolicy"]
    return [r for r in policy["spec"]["egress"]
            if any(p.get("port") == 31700 for p in r.get("ports") or [])]


def test_site_cidr_is_substituted_into_network_policy() -> None:
    (rule,) = _statics_nodeport_rules(_render())
    assert rule["to"] == [{"ipBlock": {"cidr": DOC_CIDR}}]


def test_site_cidr_none_drops_the_rule_and_keeps_policy_valid() -> None:
    rendered = _render(STATICS_DB_CIDR="none")
    assert _statics_nodeport_rules(rendered) == []
    (policy,) = [d for d in _docs(rendered) if d["kind"] == "NetworkPolicy"]
    assert len(policy["spec"]["egress"]) >= 8, "вырезан только блок между маркерами"


def test_raw_network_policy_is_rejected_by_api_server() -> None:
    """Плейсхолдер — не CIDR: сырой apply падает на валидации, а не теряет правило."""
    (policy,) = _docs((K8S / "networkpolicy.yaml").read_text(encoding="utf-8"))
    cidrs = [peer["ipBlock"]["cidr"] for r in policy["spec"]["egress"]
             for peer in r.get("to") or [] if "ipBlock" in peer]
    assert "STATICS_DB_CIDR_PLACEHOLDER" in cidrs


def test_deploy_fails_before_changes_when_site_value_is_missing() -> None:
    deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
    assert deploy.index("\nresolve_statics_cidr\n") < deploy.index("# ── 1. Секреты")


# ── RBAC ─────────────────────────────────────────────────────────────────────


def test_rbac_subjects_point_to_service_accounts_defined_here() -> None:
    """RoleBinding sre-ai-read полгода указывал на default/sre-ai — SA, которого
    нет: привязка применялась без ошибок и не давала ничего."""
    docs = _docs((K8S / "base" / "rbac.yaml").read_text(encoding="utf-8"))
    accounts = {d["metadata"]["name"] for d in docs if d["kind"] == "ServiceAccount"}
    bindings = [d for d in docs if d["kind"] in {"RoleBinding", "ClusterRoleBinding"}]
    assert bindings
    for b in bindings:
        for s in b["subjects"]:
            assert s["kind"] == "ServiceAccount" and s["name"] in accounts, b["metadata"]["name"]
            assert s["namespace"] == "sre-ai", (
                f"{b['metadata']['name']}: subject в namespace {s['namespace']!r}"
            )
        role_names = {d["metadata"]["name"] for d in docs if d["kind"] == b["roleRef"]["kind"]}
        assert b["roleRef"]["name"] in role_names, f"{b['metadata']['name']}: роли нет в файле"


# ── StatefulSet-ы ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["postgres", "redis"])
def test_stateful_images_are_pinned_by_digest(name: str) -> None:
    sts = next(d for d in _docs((K8S / f"{name}.yaml").read_text(encoding="utf-8"))
               if d["kind"] == "StatefulSet")
    (container,) = sts["spec"]["template"]["spec"]["containers"]
    assert re.fullmatch(rf"{name}:[\w.-]+@sha256:[0-9a-f]{{64}}", container["image"]), container["image"]


@pytest.mark.parametrize("name", ["postgres", "redis"])
def test_stateful_pods_are_pinned_to_the_volume_node(name: str) -> None:
    """Пин стоял в кластере патчем; `apply` манифеста без него пин снимал."""
    sts = next(d for d in _docs((K8S / f"{name}.yaml").read_text(encoding="utf-8"))
               if d["kind"] == "StatefulSet")
    (term,) = sts["spec"]["template"]["spec"]["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    assert "kubernetes.io/hostname" in {e["key"] for e in term["matchExpressions"]}
