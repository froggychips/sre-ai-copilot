import json
import re
from enum import Enum
from typing import Any, Dict, List, Optional

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
        # namespace уходит в kubectl-команду (`-n {namespace}`) и затем в argv
        # через command.split() — ровно как resource_name/label. Без charset-
        # валидации значение вроде `squad-1 -n kube-system` подменяет namespace
        # мимо guard'а (flag-инъекция). Тот же charset имён k8s namespace: ни
        # пробелов, ни ведущего '-', ни флагов. Дефолт "default" валиден.
        if not re.fullmatch(r"[a-z0-9]([a-z0-9.\-]{0,251}[a-z0-9])?", v or ""):
            raise ValueError(f"Invalid namespace: {v!r}")
        # FORBIDDEN-set проверяем ПОСЛЕ charset — оба контроля обязательны.
        if v in FORBIDDEN_NAMESPACES:
            raise ValueError(
                f"Access to namespace '{v}' is forbidden by security policy."
            )
        return v

    @field_validator("resource_name")
    @classmethod
    def validate_resource_name(cls, v: str):
        # resource_name уходит в kubectl-команду (DSLTranslator.to_kubectl) и
        # затем в argv через command.split(). Без валидации значение вроде
        # `x --namespace=kube-system` подменяет namespace мимо guard'а
        # (flag-инъекция). Ограничиваем charset'ом имён k8s-объектов: ни
        # пробелов, ни ведущего '-', ни флагов.
        if not re.fullmatch(r"[a-z0-9]([a-z0-9.\-]{0,251}[a-z0-9])?", v or ""):
            raise ValueError(f"Invalid resource_name: {v!r}")
        return v

    @field_validator("params")
    @classmethod
    def validate_params(cls, v: Dict[str, Any]):
        # params тоже попадают в argv. replicas → целое в разумном диапазоне
        # (строку-цифру коэрсим); label → селектор без пробелов/флагов.
        v = dict(v or {})
        if "replicas" in v:
            r = v["replicas"]
            if isinstance(r, str) and r.isdigit():
                r = int(r)
                v["replicas"] = r
            if isinstance(r, bool) or not isinstance(r, int) or not (1 <= r <= 100):
                raise ValueError(f"Invalid replicas: {v['replicas']!r}")
        label = v.get("label")
        if label:
            if not re.fullmatch(r"[A-Za-z0-9._/=,\-]{1,253}", str(label)):
                raise ValueError(f"Invalid label selector: {label!r}")
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
    def to_argv(intent: ExecutionIntent) -> List[str]:
        """Собрать kubectl-команду сразу как argv, без промежуточной строки.

        Почему это важнее, чем кажется. Историческая цепочка была такой:
        типизированный `ExecutionIntent` → f-строка → `command.split()` в
        `k8s_service._run_kubectl` → argv. Каждый переход терял структуру, и
        безопасность держалась на charset-регулярках у `namespace`,
        `resource_name` и `params` — их валидаторы прямо ссылаются на
        `split()` как на причину своего существования.

        Такая защита работает ровно до первого нового поля, у которого
        валидатор забыли: значение вроде `squad-1 -n kube-system` снова
        станет двумя аргументами. Здесь же элемент списка остаётся одним
        аргументом, что бы в нём ни было, — flag-инъекция перестаёт быть
        возможной структурно, а не по договорённости.

        Регулярки при этом остаются: они ловят мусор раньше и дают понятную
        ошибку вместо `kubectl` с непонятным именем.
        """
        ns = ["-n", intent.namespace]
        if intent.action == ActionType.RESTART_DEPLOYMENT:
            return ["kubectl", "rollout", "restart",
                    f"deployment/{intent.resource_name}", *ns]
        if intent.action == ActionType.SCALE_DEPLOYMENT:
            replicas = intent.params.get("replicas", 1)
            argv = ["kubectl", "scale", f"deployment/{intent.resource_name}", *ns,
                    f"--replicas={replicas}"]
            # Optimistic concurrency: `--current-replicas` — это precondition
            # на стороне kube-apiserver. Между preview (dry-run) и реальным
            # применением деплоймент мог отмасштабировать кто-то ещё: HPA,
            # соседний оператор, человек. Без precondition мы молча
            # перезаписываем чужое решение состоянием, которое видели минуту
            # назад; с ним — команда падает, и это правильный исход.
            current = intent.params.get("current_replicas")
            if current is not None:
                argv.append(f"--current-replicas={current}")
            return argv
        if intent.action == ActionType.GET_LOGS:
            return ["kubectl", "logs", intent.resource_name, *ns, "--tail=100"]
        if intent.action == ActionType.DESCRIBE_RESOURCE:
            return ["kubectl", "describe",
                    f"{intent.resource_type}/{intent.resource_name}", *ns]
        if intent.action == ActionType.GET_PODS:
            argv = ["kubectl", "get", "pods", *ns]
            label = intent.params.get("label")
            if label:
                # Отдельным элементом: значение с пробелом не расщепится.
                argv += ["-l", str(label)]
            return argv
        raise ValueError(f"Unknown action type: {intent.action!r}")

    @staticmethod
    def to_kubectl(intent: ExecutionIntent) -> str:
        """Строковая форма команды — для логов, аудита и Discord-превью.

        Собирается из `to_argv`, а не отдельным набором f-строк: две
        независимые копии одной команды разъезжались бы, и расхождение
        всплыло бы там, где строка показывается человеку («мы применим вот
        это»), а argv делает другое.
        """
        with execution_intent_span(
            action=intent.action.value,
            resource_type=intent.resource_type,
            resource_name=intent.resource_name,
            namespace=intent.namespace,
            risk=intent.risk,
            intent_json=intent.model_dump_json(),
        ):
            return " ".join(DSLTranslator.to_argv(intent))
