"""CI gate: `helm/sre-ai-copilot/files/ownership.yaml` ≡ `config/ownership.yaml`.

Helm не умеет читать файлы вне chart-dir (`.Files.Get` ограничен chart-ом),
поэтому держим **точную копию** в `helm/sre-ai-copilot/files/`. Этот тест
гарантирует, что копия не разъезжается с источником — если кто-то правит
`config/ownership.yaml` и забывает синхронизировать helm-копию, configmap
в runtime будет stale.

Чинить так:

    cp config/ownership.yaml helm/sre-ai-copilot/files/ownership.yaml

См. docs/RUNBOOK.md § "Activate *-shared ownership manifest in runtime".
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SOURCE = _REPO_ROOT / "config" / "ownership.yaml"
_HELM_COPY = _REPO_ROOT / "helm" / "sre-ai-copilot" / "files" / "ownership.yaml"


def test_helm_ownership_copy_exists() -> None:
    """Helm chart должен включать копию manifest-а в `files/`."""
    assert _HELM_COPY.exists(), (
        f"missing helm copy {_HELM_COPY}; run "
        f"`cp {_SOURCE.relative_to(_REPO_ROOT)} "
        f"{_HELM_COPY.relative_to(_REPO_ROOT)}`"
    )


def test_helm_ownership_byte_identical() -> None:
    """Файлы должны быть байт-в-байт одинаковы.

    YAML-структурного сравнения недостаточно: комментарии и порядок rules
    важны (rule-matching — first-wins, см. ownership_suggester._manifest_match).
    """
    source_bytes = _SOURCE.read_bytes()
    helm_bytes = _HELM_COPY.read_bytes()
    if source_bytes != helm_bytes:
        pytest.fail(
            "config/ownership.yaml and helm/sre-ai-copilot/files/ownership.yaml "
            "are out of sync. Run:\n\n"
            f"    cp {_SOURCE.relative_to(_REPO_ROOT)} "
            f"{_HELM_COPY.relative_to(_REPO_ROOT)}\n"
        )


def test_helm_ownership_parses_as_list_of_rules() -> None:
    """Sanity: helm-копия — валидный YAML-список правил с обязательными полями."""
    data = yaml.safe_load(_HELM_COPY.read_text(encoding="utf-8"))
    assert isinstance(data, list) and data, "expected non-empty list of rules"
    for i, rule in enumerate(data):
        assert isinstance(rule, dict), f"rule[{i}] must be dict, got {type(rule)}"
        assert "ns_pattern" in rule, f"rule[{i}] missing ns_pattern"
        assert "owner" in rule, f"rule[{i}] missing owner"
