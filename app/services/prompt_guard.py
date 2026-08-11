"""Best-effort ТЕЛЕМЕТРИЯ по prompt injection. НЕ security control.

Читать до того, как полагаться на этот модуль.

ЧТО ЗДЕСЬ ЕСТЬ. Горстка англоязычных regex-ов (`ignore previous
instructions`, `dan mode`, `<|endoftext|>`, …) и экранирование
`<user_context>`-разделителей. Всё.

ЧЕГО ЗДЕСЬ НЕТ И НЕ БУДЕТ. Это НЕ фильтр атак. Обходится тривиально:
другим языком («забудь предыдущие инструкции»), парафразом, base64/юникодным
гомоглифом, косвенной инъекцией (текст приезжает не от человека, а из
annotations алерта, Seq-лога, коммит-месседжа, Jira-тикета). Расширять
regex-список НЕ НАДО: каждый новый паттерн повышает уверенность в защите,
которой нет, а обход остаётся ценой одной перефразировки. Ценность модуля —
СИГНАЛ в логах («в контекст приехало что-то похожее на попытку перехвата»),
а не граница безопасности.

ЧТО РЕАЛЬНО ДЕРЖИТ ГРАНИЦУ (защита от «LLM убедили сделать kubectl»):

  * `app/remediation/executor_gate.py::evaluate_intent_gate` —
    ДЕТЕРМИНИРОВАННЫЙ policy-gate: риск пересчитывается из самого
    ExecutionIntent, а не из LLM-поля `risk`; BLOCK не обходится текстом.
  * `app/services/executor_apply.py` — server-side namespace-binding
    (`intent.namespace` обязан совпасть с namespace инцидента из БД, поэтому
    инжектированный intent не может действовать в чужом ns), обязательная
    подпись intent-а (TOCTOU), окна свежести approval-а и самого плана,
    повторный `--dry-run=server` перед записью.
  * ОБЯЗАТЕЛЬНЫЙ человеческий APPROVED в `kg_action_approvals` — терминальная
    запись проверяется независимо от LLM-пути.
  * `app/security/namespaces.py` + `app/services/k8s_guard.py` — forbidden /
    read-only tiers: prod-* недоступен для записи в принципе.

Инвариант: любая инъекция, прошедшая мимо этих regex-ов (то есть почти
любая), должна упираться в перечисленное выше. Если когда-нибудь окажется,
что единственное, что остановило инъекцию, — это `detect_injection`, то
сломан не regex, а executor-gate. См. SECURITY.md, раздел
«prompt_guard — telemetry, not a control».
"""
import re
from typing import Tuple

import structlog

logger = structlog.get_logger()


class PromptGuard:
    # Паттерны атак: "ignore previous instructions", "jailbreak", "override".
    # Это ЕДИНСТВЕННЫЙ блокирующий сигнал — настоящие попытки перехвата
    # инструкций модели.
    # Между глаголом и «(previous|...)» допускаем НЕСКОЛЬКО артиклей/
    # квантификаторов («ignore the previous...», «forget all of the prior...»):
    # жёсткое `(all\s+)?` пропускало тривиальные перефразировки.
    INJECTION_PATTERNS = [
        r"(ignore|disregard|forget)\s+(?:(?:all|any|the|your|these|those|of)\s+)*"
        r"(previous|prior|initial|earlier|above|preceding|system)\s+"
        r"(instructions|prompts|rules|directives|messages|context)",
        r"you\s+are\s+now\s+a\s+(developer|hacker|unrestricted\s+ai)",
        r"new\s+rule:",
        r"set\s+your\s+output\s+format\s+to",
        r"dan\s+mode",
        r"<\|endoftext\|>",
        r"ignore\s+all\s+rules",
    ]

    # Паттерны "похоже на код" (Python/Bash). НЕ блокирующие: данные инцидентов
    # (стектрейсы .NET/Python/Go, логи краша) легитимно содержат `import os`,
    # `eval(`, `subprocess.` и т.п. Оставлены только как best-effort warning-лог
    # для телеметрии, PermissionError по ним НЕ кидаем.
    CODE_PATTERNS = [r"import\s+os", r"subprocess\.", r"rm\s+-rf", r"eval\(", r"exec\("]

    @classmethod
    def sanitize(cls, user_input: str) -> str:
        """
        Базовая очистка, экранирование разделителей и ОБРЕЗКА по размеру.

        Размер ввода больше НЕ блокируется (это роняло крупные, но легитимные
        инциденты с большим teamcity_context / логами). Вместо отказа длинный
        ввод обрезается до PROMPT_INPUT_MAX_CHARS с маркером.
        """
        # Предотвращаем закрытие И открытие XML-тегов пользователем: раньше
        # экранировался только `</user_context>`, а открывающий тег позволял
        # вклинить фальшивый вложенный контекст.
        sanitized = user_input.replace("</user_context>", "[TAG_ESCAPE]")
        sanitized = sanitized.replace("<user_context>", "[TAG_ESCAPE]")
        sanitized = sanitized.replace("]]>", "[CDATA_ESCAPE]")
        sanitized = sanitized.strip()

        from app.config import settings as _settings

        max_chars = getattr(_settings, "PROMPT_INPUT_MAX_CHARS", 20000)
        if len(sanitized) > max_chars:
            dropped = len(sanitized) - max_chars
            logger.info(
                "prompt_guard.input_truncated",
                original_len=len(sanitized),
                max_chars=max_chars,
                dropped_chars=dropped,
            )
            sanitized = sanitized[:max_chars] + f"…[truncated {dropped} chars]"

        return sanitized

    @classmethod
    def detect_injection(cls, user_input: str) -> Tuple[bool, str]:
        """
        Проверяет ввод на признаки Prompt Injection.

        Блокирует ТОЛЬКО реальные попытки перехвата инструкций
        (INJECTION_PATTERNS). Размер ввода и "похожий на код" контент НЕ
        являются атакой — размер обрабатывается обрезкой в sanitize(),
        код-паттерны логируются как best-effort warning без блокировки.

        True здесь означает «сработала эвристика», а False — НЕ «инъекции
        нет»: ложноотрицательных тут много by design (другой язык, парафраз,
        косвенная инъекция). Полагаться на этот результат как на границу
        безопасности нельзя — см. докстринг модуля.
        """
        cleaned = user_input.lower()

        for pattern in cls.INJECTION_PATTERNS:
            if re.search(pattern, cleaned):
                return True, "INSTRUCTION_OVERRIDE_ATTEMPT"

        # Best-effort телеметрия: код-паттерны НЕ блокируем (легитимны в
        # стектрейсах/логах краша), только отмечаем в логе.
        for pattern in cls.CODE_PATTERNS:
            if re.search(pattern, cleaned):
                logger.debug(
                    "prompt_guard.code_pattern_seen",
                    pattern=pattern,
                )
                break

        return False, ""


prompt_guard = PromptGuard()
