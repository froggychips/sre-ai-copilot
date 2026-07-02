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
READ_ONLY_NAMESPACES: Set[str] = {"prod", "preprod", "preupdate"}

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
    return _normalize(ns) in READ_ONLY_NAMESPACES
