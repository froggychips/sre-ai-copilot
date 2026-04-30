import re
import structlog
from typing import Tuple

logger = structlog.get_logger()

class PromptGuard:
    # Паттерны атак: "ignore previous instructions", "jailbreak", "override"
    INJECTION_PATTERNS = [
        r"(ignore|disregard|forget)\s+(all\s+)?(previous|prior|initial)\s+(instructions|prompts)",
        r"you\s+are\s+now\s+a\s+(developer|hacker|unrestricted\s+ai)",
        r"new\s+rule:",
        r"set\s+your\s+output\s+format\s+to",
        r"dan\s+mode",
        r"<\|endoftext\|>", 
    ]

    # Паттерны попыток выполнить код (Python/Bash)
    CODE_PATTERNS = [
        r"import\s+os", r"subprocess\.", r"rm\s+-rf", r"eval\(", r"exec\("
    ]

    @classmethod
    def sanitize(cls, user_input: str) -> str:
        """
        Базовая очистка и экранирование разделителей.
        """
        # Предотвращаем закрытие XML тегов пользователем
        sanitized = user_input.replace("</user_context>", "[TAG_ESCAPE]")
        sanitized = sanitized.replace("]]>", "[CDATA_ESCAPE]")
        return sanitized.strip()

    @classmethod
    def detect_injection(cls, user_input: str) -> Tuple[bool, str]:
        """
        Проверяет ввод на наличие признаков Prompt Injection.
        """
        cleaned = user_input.lower()
        
        for pattern in cls.INJECTION_PATTERNS:
            if re.search(pattern, cleaned):
                return True, "INSTRUCTION_OVERRIDE_ATTEMPT"

        for pattern in cls.CODE_PATTERNS:
            if re.search(pattern, cleaned):
                return True, "CODE_INJECTION_ATTEMPT"

        if len(user_input) > 5000: # Лимит для предотвращения DoS
            return True, "INPUT_TOO_LONG"

        return False, ""

prompt_guard = PromptGuard()
