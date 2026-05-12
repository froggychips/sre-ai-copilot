import json
from typing import Optional, Set

import structlog
from opentelemetry import trace
from pydantic import BaseModel

from app.security.namespaces import (
    FORBIDDEN_NAMESPACES,
    READ_ONLY_NAMESPACES,
    is_write_namespace,
)

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


class K8sOperation(BaseModel):
    verb: str
    resource: str
    namespace: str
    name: Optional[str] = None
    body: Optional[dict] = None


class K8sSecurityGuard:
    @classmethod
    def _is_write_namespace(cls, ns: str) -> bool:
        return is_write_namespace(ns)

    @classmethod
    def _guard_block(cls, reason: str, **attrs) -> None:
        """Emits a guardrail.blocked OTEL event on the current span and logs."""
        span = trace.get_current_span()
        span.add_event(
            "guardrail.blocked",
            attributes={"sre.guardrail.reason": reason, "sre.guardrail.decision": "blocked", **attrs},
        )
        span.set_attribute("sre.guardrail.decision", "blocked")

    @classmethod
    def validate(cls, op: K8sOperation) -> bool:
        verb = op.verb.lower()
        ns = op.namespace

        if ns in FORBIDDEN_NAMESPACES:
            cls._guard_block("namespace_forbidden", **{"sre.namespace": ns})
            logger.error("security_violation_namespace_forbidden", ns=ns)
            raise PermissionError(
                f"Access to namespace '{ns}' is blocked by security policy."
            )

        if op.resource.lower() not in ALLOWED_RESOURCES:
            cls._guard_block("resource_not_allowed", **{"sre.resource": op.resource})
            logger.error("security_violation_resource", resource=op.resource)
            raise PermissionError(
                f"Resource '{op.resource}' is not in the approved list."
            )

        if verb in WRITE_VERBS:
            if ns in READ_ONLY_NAMESPACES:
                cls._guard_block("write_to_readonly_ns", **{"sre.namespace": ns, "sre.verb": verb})
                logger.error(
                    "security_violation_write_to_readonly_ns", ns=ns, verb=verb
                )
                raise PermissionError(
                    f"Write actions are not permitted in '{ns}' (read-only tier)."
                )
            if not cls._is_write_namespace(ns):
                cls._guard_block("write_outside_squad", **{"sre.namespace": ns, "sre.verb": verb})
                logger.error("security_violation_write_outside_squad", ns=ns, verb=verb)
                raise PermissionError(
                    f"Write actions are only permitted in squad-* namespaces, not '{ns}'."
                )
        elif verb not in READ_ONLY_VERBS:
            cls._guard_block("verb_not_allowed", **{"sre.verb": verb})
            logger.error("security_violation_verb", verb=verb)
            raise PermissionError(
                f"Action '{verb}' is not permitted for AI-driven operations."
            )

        if op.body:
            body_str = json.dumps(op.body).lower()
            if '"privileged": true' in body_str:
                cls._guard_block("privileged_container")
                logger.error("security_violation_privileged_container")
                raise PermissionError("Privileged containers are strictly forbidden.")
            if '"hostnetwork": true' in body_str:
                cls._guard_block("host_network")
                logger.error("security_violation_host_network")
                raise PermissionError(
                    "hostNetwork usage is blocked for security reasons."
                )

        span = trace.get_current_span()
        span.add_event(
            "guardrail.passed",
            attributes={
                "sre.guardrail.decision": "allowed",
                "sre.verb": verb,
                "sre.resource": op.resource,
                "sre.namespace": ns,
            },
        )
        logger.info("k8s_guard_passed", verb=verb, resource=op.resource, ns=ns)
        return True


k8s_guard = K8sSecurityGuard()
