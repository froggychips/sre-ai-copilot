import pytest

from app.services.k8s_guard import K8sOperation, k8s_guard


def op(
    verb: str, ns: str, resource: str = "pods", body: dict | None = None
) -> K8sOperation:
    return K8sOperation(verb=verb, resource=resource, namespace=ns, body=body)


def test_read_in_prod_allowed():
    assert k8s_guard.validate(op("get", "prod"))
    assert k8s_guard.validate(op("list", "preprod"))
    assert k8s_guard.validate(op("watch", "preupdate"))


def test_write_in_prod_blocked():
    with pytest.raises(PermissionError, match="read-only tier"):
        k8s_guard.validate(op("patch", "prod"))
    with pytest.raises(PermissionError, match="read-only tier"):
        k8s_guard.validate(op("create", "preprod"))


def test_write_in_squad_allowed():
    assert k8s_guard.validate(op("patch", "squad-3"))
    assert k8s_guard.validate(op("create", "squad-gd"))


def test_write_in_unknown_ns_blocked():
    with pytest.raises(PermissionError, match="squad-\\* namespaces"):
        k8s_guard.validate(op("patch", "ai-platform"))


def test_forbidden_namespaces():
    for ns in ("kube-system", "kube-public", "mcp", "chaos-mesh"):
        with pytest.raises(PermissionError, match="blocked by security policy"):
            k8s_guard.validate(op("get", ns))


def test_unknown_verb_rejected():
    with pytest.raises(PermissionError, match="not permitted"):
        k8s_guard.validate(op("delete", "squad-1"))


def test_unknown_resource_rejected():
    with pytest.raises(PermissionError, match="approved list"):
        k8s_guard.validate(op("get", "prod", resource="secrets"))


def test_privileged_container_blocked():
    with pytest.raises(PermissionError, match="[Pp]rivileged"):
        k8s_guard.validate(op("create", "squad-1", body={"spec": {"privileged": True}}))


def test_host_network_blocked():
    with pytest.raises(PermissionError, match="hostNetwork"):
        k8s_guard.validate(
            op("create", "squad-1", body={"spec": {"hostNetwork": True}})
        )
