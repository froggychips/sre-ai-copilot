import structlog
from typing import Optional, Set
from pydantic import BaseModel

logger = structlog.get_logger()

class K8sOperation(BaseModel):
    verb: str
    resource: str
    namespace: str
    name: Optional[str] = None
    body: Optional[dict] = None

class K8sSecurityGuard:
    # Ограничиваем список разрешенных действий для AI
    ALLOWED_VERBS: Set[str] = {"get", "list", "watch", "patch", "create"}
    FORBIDDEN_NAMESPACES: Set[str] = {"kube-system", "kube-public", "kube-node-lease", "chaos-mesh"}
    ALLOWED_RESOURCES: Set[str] = {"pods", "deployments", "services", "configmaps", "ingresses"}

    @classmethod
    def validate(cls, op: K8sOperation) -> bool:
        """
        Выполняет многоуровневую проверку безопасности перед вызовом K8s API.
        """
        # 1. Проверка Namespace
        if op.namespace in cls.FORBIDDEN_NAMESPACES:
            logger.error("security_violation_namespace", ns=op.namespace)
            raise PermissionError(f"Access to namespace '{op.namespace}' is blocked by security policy.")

        # 2. Проверка Verb
        if op.verb.lower() not in cls.ALLOWED_VERBS:
            logger.error("security_violation_verb", verb=op.verb)
            raise PermissionError(f"Action '{op.verb}' is not permitted for AI-driven operations.")

        # 3. Проверка Resource
        if op.resource.lower() not in cls.ALLOWED_RESOURCES:
            logger.error("security_violation_resource", resource=op.resource)
            raise PermissionError(f"Resource '{op.resource}' is not in the approved list.")

        # 4. Проверка содержимого (Deep Inspection)
        if op.body:
            body_str = str(op.body).lower()
            # Запрет привилегированных контейнеров
            if "privileged: true" in body_str:
                logger.error("security_violation_privileged_container")
                raise PermissionError("Privileged containers are strictly forbidden.")
            
            # Запрет использования hostNetwork
            if "hostnetwork: true" in body_str:
                logger.error("security_violation_host_network")
                raise PermissionError("hostNetwork usage is blocked for security reasons.")

        logger.info("k8s_guard_passed", verb=op.verb, resource=op.resource, ns=op.namespace)
        return True

k8s_guard = K8sSecurityGuard()
