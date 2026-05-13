import json
import re
from enum import Enum
from typing import Any, Dict, Optional

import structlog
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.security.namespaces import FORBIDDEN_NAMESPACES
from app.services.telemetry_utils import execution_intent_span

logger = structlog.get_logger()

# Ловит первый JSON-объект в тексте: { ... } (с поддержкой вложенности до 1 уровня).
# LLM иногда оборачивает JSON в ```json``` fences или добавляет лидер-текст.
_JSON_OBJ_RE = re.compile(r"\{(?:[^{}]|\{[^{}]*\})*\}", re.DOTALL)


class ActionType(str, Enum):
    RESTART_DEPLOYMENT = "restart_deployment"
    SCALE_DEPLOYMENT = "scale_deployment"
    GET_LOGS = "get_logs"
    DESCRIBE_RESOURCE = "describe_resource"
    GET_PODS = "get_pods"


class ExecutionIntent(BaseModel):
    action: ActionType
    resource_type: str = Field(..., pattern="^(deployment|pod|service|ingress)$")
    resource_name: str
    namespace: str = "default"
    params: Dict[str, Any] = Field(default_factory=dict)
    risk: str = "medium"

    @field_validator("namespace")
    @classmethod
    def block_system_ns(cls, v: str):
        if v in FORBIDDEN_NAMESPACES:
            raise ValueError(
                f"Access to namespace '{v}' is forbidden by security policy."
            )
        return v

    @classmethod
    def from_llm_response(cls, text: str) -> Optional["ExecutionIntent"]:
        """Распарсить JSON ExecutionIntent из LLM-ответа.

        Допускает:
          - чистый JSON-объект
          - JSON в ```json ... ``` или ``` ... ``` code-fence
          - prose с встроенным JSON-блоком

        Возвращает None если:
          - JSON не найден / невалидный
          - pydantic-валидация не прошла (включая FORBIDDEN_NAMESPACES)

        Никогда не бросает — caller-у проще обрабатывать advisory-режим.
        """
        if not text or not text.strip():
            return None

        # Снимаем code-fence, если есть.
        cleaned = text.strip()
        if cleaned.startswith("```"):
            # ```json\n...\n``` или ```\n...\n```
            cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```\s*$", "", cleaned)

        # Прямой парс — нормальный happy path по нашему prompt-у.
        try:
            obj = json.loads(cleaned)
            return cls.model_validate(obj)
        except (json.JSONDecodeError, ValidationError):
            pass

        # Fallback: вытащить первый {...} из текста (LLM добавил prose-обёртку).
        match = _JSON_OBJ_RE.search(text)
        if not match:
            logger.warning("execution_intent.no_json_found", text_preview=text[:200])
            return None
        try:
            obj = json.loads(match.group(0))
            return cls.model_validate(obj)
        except json.JSONDecodeError as e:
            logger.warning(
                "execution_intent.json_invalid",
                error=str(e),
                snippet=match.group(0)[:200],
            )
            return None
        except ValidationError as e:
            logger.warning(
                "execution_intent.schema_invalid",
                error=str(e),
                snippet=match.group(0)[:200],
            )
            return None


class DSLTranslator:
    @staticmethod
    def to_kubectl(intent: ExecutionIntent) -> str:
        """Generate kubectl command from intent and record an audit span."""
        with execution_intent_span(
            action=intent.action.value,
            resource_type=intent.resource_type,
            resource_name=intent.resource_name,
            namespace=intent.namespace,
            risk=intent.risk,
            intent_json=intent.model_dump_json(),
        ):
            mapping = {
                ActionType.RESTART_DEPLOYMENT: (
                    f"kubectl rollout restart deployment/{intent.resource_name} "
                    f"-n {intent.namespace}"
                ),
                ActionType.SCALE_DEPLOYMENT: (
                    f"kubectl scale deployment/{intent.resource_name} "
                    f"-n {intent.namespace} "
                    f"--replicas={intent.params.get('replicas', 1)}"
                ),
                ActionType.GET_LOGS: (
                    f"kubectl logs {intent.resource_name} -n {intent.namespace} --tail=100"
                ),
                ActionType.DESCRIBE_RESOURCE: (
                    f"kubectl describe {intent.resource_type}/{intent.resource_name} "
                    f"-n {intent.namespace}"
                ),
                ActionType.GET_PODS: (
                    f"kubectl get pods -n {intent.namespace} "
                    f"-l {intent.params.get('label', '')}"
                ),
            }
            cmd = mapping.get(intent.action)
            if cmd is None:
                raise ValueError(f"Unknown action type: {intent.action!r}")
            return cmd
