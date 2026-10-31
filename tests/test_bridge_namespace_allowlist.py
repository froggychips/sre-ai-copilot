"""Поэтапное включение моста: REMEDIATION_BRIDGE_NAMESPACES (инцидент 31.10.2026)."""
import pytest

from app.config import settings
from app.remediation.bridge_dispatch import namespace_allowed


@pytest.mark.parametrize("raw,ns,ok", [
    ("", "squad-49-shared", True),
    ("", None, True),
    ("squad-49-", "squad-49-shared", True),
    ("squad-49-", "squad-4-shared", False),
    ("squad-49-, squad-5-", "squad-5-kingdom7", True),
    ("squad-49-", None, False),
    ("squad-49-", "prod-shared", False),
])
def test_namespace_allowlist(monkeypatch, raw, ns, ok):
    monkeypatch.setattr(settings, "REMEDIATION_BRIDGE_NAMESPACES", raw)
    assert namespace_allowed(ns) is ok
