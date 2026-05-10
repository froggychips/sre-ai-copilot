import pytest
from app.context.k8s_facts import K8sFacts


def test_peer_squad1_returns_squad2():
    assert K8sFacts._peer_namespace("squad-1-kingdom2") == "squad-2-kingdom2"


def test_peer_squad2_returns_squad3():
    assert K8sFacts._peer_namespace("squad-2-shared") == "squad-3-shared"


def test_peer_squad3_returns_squad2():
    assert K8sFacts._peer_namespace("squad-3-auth") == "squad-2-auth"


def test_peer_squad4_returns_squad2():
    assert K8sFacts._peer_namespace("squad-4-payments") == "squad-2-payments"


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
    assert K8sFacts._peer_namespace("squad-1-very-long-suffix") == "squad-2-very-long-suffix"
