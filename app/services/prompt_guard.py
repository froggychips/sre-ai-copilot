import re
from typing import Tuple

import structlog

logger = structlog.get_logger()


class PromptGuard:
    # Паттерны атак: "ignore previous instructions", "jailbreak", "override".
    # Это ЕДИНСТВЕННЫЙ блокирующий сигнал — настоящие попытки перехвата
    # инструкций модели.
    INJECTION_PATTERNS = [
        r"(ignore|disregard|forget)\s+(all\s+)?(previous|prior|initial)\s+(instructions|prompts)",
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
        # Предотвращаем закрытие XML тегов пользователем
        sanitized = user_input.replace("</user_context>", "[TAG_ESCAPE]")
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
