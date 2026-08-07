"""Единый whitelist/blacklist для kubernetes namespace-ов.

Два разных списка в k8s_guard.py и core/execution_dsl.py расходились
(второй забывал про kube-node-lease и mcp). Любая правка — здесь, оба
потребителя обязаны импортировать только эти константы.
"""
import re
from typing import Pattern, Set

# Доступ запрещён напрямую и через DSL — kubectl exec/delete/scale тоже.
FORBIDDEN_NAMESPACES: Set[str] = {
    "kube-system",
    "kube-public",
    "kube-node-lease",
    "chaos-mesh",
    "mcp",  # MCP auth/tooling — никогда не трогать
}

# Production-tier namespaces — только read-only (никакого scale/delete/patch).
# Реальные ns называются `prod-kingdom5` / `preupdate-shared` / `preprod-qa-1`
# и т.п. — точный `in`-матч по базовым именам их НЕ ловил, ветка была мёртвой.
# Матчим точное имя ИЛИ префикс `<env>-` (см. is_read_only ниже).
READ_ONLY_NAMESPACES: Set[str] = {"prod", "preprod", "preupdate", "production"}
READ_ONLY_NAMESPACE_PREFIXES: tuple[str, ...] = tuple(
    f"{env}-" for env in sorted(READ_ONLY_NAMESPACES)
)

# Где разрешена запись (после approval).
WRITE_NAMESPACE_PATTERNS: list[Pattern[str]] = [
    re.compile(r"^squad-\d+$"),
    re.compile(r"^squad-gd$"),
]


def _normalize(ns: str) -> str:
    # kubectl само нормализует namespace через command.split() (обрезает
    # окружающие пробелы), а k8s namespace-имена всегда lowercase. Значение
    # вроде "Kube-System" или "kube-system " (хвостовой пробел) обходит точный
    # `in`-матч, но kubectl вернёт его к "kube-system". Сводим к тому же виду
    # ДО сравнения, чтобы обход был невозможен. Значения множеств не трогаем.
    return (ns or "").strip().lower()


def is_write_namespace(ns: str) -> bool:
    return any(p.match(_normalize(ns)) for p in WRITE_NAMESPACE_PATTERNS)


def is_forbidden(ns: str) -> bool:
    return _normalize(ns) in FORBIDDEN_NAMESPACES


def is_read_only(ns: str) -> bool:
    # Точное имя ("prod") или префикс ("prod-kingdom5", "preupdate-shared").
    # ВАЖНО: это дополнительный запрет — allowlist записи
    # (WRITE_NAMESPACE_PATTERNS) не ослабляется.
    n = _normalize(ns)
    return n in READ_ONLY_NAMESPACES or n.startswith(READ_ONLY_NAMESPACE_PREFIXES)
