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


def test_write_in_prod_prefixed_ns_blocked_as_read_only():
    """Реальные ns называются prod-kingdom5/preupdate-shared — точный матч
    по "prod"/"preprod"/"preupdate" их не ловил, и read-only ветка была
    мёртвой. Теперь префикс-матч даёт именно read-only-tier отказ."""
    for ns in ("prod-kingdom5", "preprod-shared", "preupdate-shared",
               "prod-isolated", "preprod-qa-1"):
        with pytest.raises(PermissionError, match="read-only tier"):
            k8s_guard.validate(op("patch", ns))


def test_read_in_prod_prefixed_ns_allowed():
    """Read-only tier ограничивает только запись — get/list в prod-* можно."""
    assert k8s_guard.validate(op("get", "prod-kingdom5"))
    assert k8s_guard.validate(op("list", "preupdate-shared"))


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


def test_forbidden_namespaces_case_and_whitespace_normalized():
    """Обход через регистр/пробелы ("Kube-System", "kube-system ") не проходит."""
    for ns in ("Kube-System", "kube-system ", " kube-system", "KUBE-SYSTEM", "MCP", "Chaos-Mesh"):
        with pytest.raises(PermissionError, match="blocked by security policy"):
            k8s_guard.validate(op("get", ns))


def test_readonly_namespaces_case_and_whitespace_normalized():
    """Запись в prod-tier блокируется даже при "Prod"/"prod " (нормализация)."""
    for ns in ("Prod", "prod ", " PREPROD", "PreUpdate"):
        with pytest.raises(PermissionError, match="read-only tier"):
            k8s_guard.validate(op("patch", ns))


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
