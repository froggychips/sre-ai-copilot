
from app.context.k8s_facts import K8sFacts


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
