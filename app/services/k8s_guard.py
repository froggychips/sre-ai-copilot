import json
import re
from typing import Optional, Set

import structlog
from pydantic import BaseModel

logger = structlog.get_logger()

READ_ONLY_VERBS: Set[str] = {"get", "list", "watch"}
WRITE_VERBS: Set[str] = {"patch", "create"}
ALLOWED_RESOURCES: Set[str] = {
    "pods",
    "deployments",
    "services",
    "configmaps",
    "ingresses",
}

# WO cluster (lastoasisgame-local) namespace policy.
# Production-tier namespaces are read-only for AI; squad-* dev environments
# allow writes (still gated by approval flow upstream); system/auth namespaces
# are forbidden outright.
FORBIDDEN_NAMESPACES: Set[str] = {
    "kube-system",
    "kube-public",
    "kube-node-lease",
    "chaos-mesh",
    "mcp",  # MCP auth/tooling — never touch
}
READ_ONLY_NAMESPACES: Set[str] = {"prod", "preprod", "preupdate"}
WRITE_NAMESPACE_PATTERNS = [re.compile(r"^squad-\d+$"), re.compile(r"^squad-gd$")]


class K8sOperation(BaseModel):
    verb: str
    resource: str
    namespace: str
    name: Optional[str] = None
    body: Optional[dict] = None


class K8sSecurityGuard:
    @classmethod
    def _is_write_namespace(cls, ns: str) -> bool:
        return any(p.match(ns) for p in WRITE_NAMESPACE_PATTERNS)

    @classmethod
    def validate(cls, op: K8sOperation) -> bool:
        verb = op.verb.lower()
        ns = op.namespace

        if ns in FORBIDDEN_NAMESPACES:
            logger.error("security_violation_namespace_forbidden", ns=ns)
            raise PermissionError(
                f"Access to namespace '{ns}' is blocked by security policy."
            )

        if op.resource.lower() not in ALLOWED_RESOURCES:
            logger.error("security_violation_resource", resource=op.resource)
            raise PermissionError(
                f"Resource '{op.resource}' is not in the approved list."
            )

        if verb in WRITE_VERBS:
            if ns in READ_ONLY_NAMESPACES:
                logger.error(
                    "security_violation_write_to_readonly_ns", ns=ns, verb=verb
                )
                raise PermissionError(
                    f"Write actions are not permitted in '{ns}' (read-only tier)."
                )
            if not cls._is_write_namespace(ns):
                logger.error("security_violation_write_outside_squad", ns=ns, verb=verb)
                raise PermissionError(
                    f"Write actions are only permitted in squad-* namespaces, not '{ns}'."
                )
        elif verb not in READ_ONLY_VERBS:
            logger.error("security_violation_verb", verb=verb)
            raise PermissionError(
                f"Action '{verb}' is not permitted for AI-driven operations."
            )

        if op.body:
            body_str = json.dumps(op.body).lower()
            if '"privileged": true' in body_str:
                logger.error("security_violation_privileged_container")
                raise PermissionError("Privileged containers are strictly forbidden.")
            if '"hostnetwork": true' in body_str:
                logger.error("security_violation_host_network")
                raise PermissionError(
                    "hostNetwork usage is blocked for security reasons."
                )

        logger.info("k8s_guard_passed", verb=verb, resource=op.resource, ns=ns)
        return True


k8s_guard = K8sSecurityGuard()
