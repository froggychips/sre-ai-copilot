"""TC username → team mapping для owner inference.

Используется в `ownership_suggester.suggest_owner_multi_signal` сигнал B
(deploy_history): most-frequent `triggered_by` за 30 дней транслируется в
`@squad-N` / `@platform`.

Источники маппинга (в порядке приоритета):
  1. YAML-файл из ENV `OWNER_ALIASES_PATH` (если задан и существует) —
     deployment-specific override.
  2. Дефолты в `_DEFAULT_ALIASES` ниже — то что мы стабильно знаем по WO.
  3. Fallback `@?-{username}` — caller вернёт это для неизвестных юзеров.

Формат YAML:
    kemyashev: "@squad-1"
    apleshkov: "@squad-2"
    wizaryx: "@platform"

Все ключи lower-case (TC usernames исторически lower-case).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, Optional

import yaml

log = logging.getLogger(__name__)


# Дефолтный pre-baked маппинг — то что подтверждено по recent_deploys digest-у
# и наблюдениям в TC (см. ref_wo_addnode_prepare_ssh_pre_step / project_zakhar*).
# Расширять по мере обнаружения. Юнит — без @ префикса в values, добавляется в
# caller-е чтобы строка не зависела от YAML-парсинга.
_DEFAULT_ALIASES: Dict[str, str] = {
    "kemyashev": "@squad-1",
    "apleshkov": "@squad-2",
    "wizaryx": "@platform",
}


_FILE_CACHE: Optional[Dict[str, str]] = None
_FILE_CACHE_PATH: Optional[str] = None


def _load_yaml_aliases(path: str) -> Dict[str, str]:
    """Прочитать YAML, отвалидировать формат, нормализовать ключи в lower-case.

    Невалидный YAML / отсутствующий файл / не-dict содержимое → пустой dict
    (с warning'ом). Caller остаётся на дефолтах.
    """
    try:
        p = Path(path)
        if not p.exists():
            return {}
        with p.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        if not isinstance(raw, dict):
            log.warning("owner_aliases: %s — ожидался dict, получили %s", path, type(raw).__name__)
            return {}
        out: Dict[str, str] = {}
        for k, v in raw.items():
            if not isinstance(k, str) or not isinstance(v, str):
                continue
            out[k.lower()] = v
        return out
    except Exception as e:
        log.warning("owner_aliases: не смог прочитать %s: %s", path, e)
        return {}


def get_aliases() -> Dict[str, str]:
    """Вернуть объединённый маппинг username (lower) → team string (с @).

    File overrides defaults. Кэшируем по пути файла — повторные вызовы не
    читают diskdrive каждый раз.
    """
    global _FILE_CACHE, _FILE_CACHE_PATH

    path = os.environ.get("OWNER_ALIASES_PATH", "").strip()

    file_aliases: Dict[str, str] = {}
    if path:
        if _FILE_CACHE is not None and _FILE_CACHE_PATH == path:
            file_aliases = _FILE_CACHE
        else:
            file_aliases = _load_yaml_aliases(path)
            _FILE_CACHE = file_aliases
            _FILE_CACHE_PATH = path

    merged: Dict[str, str] = {}
    # сначала дефолты — file overrides
    for k, v in _DEFAULT_ALIASES.items():
        merged[k.lower()] = v
    for k, v in file_aliases.items():
        merged[k] = v
    return merged


def resolve_username(username: str) -> str:
    """username → owner string (`@squad-N` / `@platform` / fallback `@?-username`).

    Пустой / None → `@?` (нет атрибуции).
    """
    if not username:
        return "@?"
    aliases = get_aliases()
    key = username.lower().strip()
    if key in aliases:
        return aliases[key]
    return f"@?-{key}"


def reset_cache() -> None:
    """Тестовый хелпер — сбросить in-process кэш файла."""
    global _FILE_CACHE, _FILE_CACHE_PATH
    _FILE_CACHE = None
    _FILE_CACHE_PATH = None
