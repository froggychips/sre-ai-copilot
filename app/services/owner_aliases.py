"""TC username → team mapping для owner inference.

Используется в `ownership_suggester.suggest_owner_multi_signal` сигнал B
(deploy_history): most-frequent `triggered_by` за 30 дней транслируется в
`@squad-N` / `@platform`.

Источники маппинга (в порядке приоритета):
  1. YAML-файл из ENV `OWNER_ALIASES_PATH` (если задан и существует) —
     deployment-specific override.
  2. Bundled YAML `app/services/owner_aliases.yaml` рядом с модулем —
     версионируется в репо, расширяется через PR.
  3. Дефолты в `_DEFAULT_ALIASES` ниже — minimal hardcode (можно удалить
     когда bundled YAML стабилизируется).
  4. Fallback `@?-{username}` — caller вернёт это для неизвестных юзеров.

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


# Дефолтный pre-baked маппинг — to-be-deprecated после полного перехода на
# bundled YAML. Оставлено как safety-net на случай отсутствия yaml-файла.
# Каноничный source-of-truth — `owner_aliases.yaml` рядом с модулем.
_DEFAULT_ALIASES: Dict[str, str] = {
    "kemyashev": "@squad-1",
    "apleshkov": "@squad-2",
    "wizaryx": "@platform",
}


# Путь к bundled YAML рядом с модулем. Читается без ENV-флага если файл есть.
# ENV `OWNER_ALIASES_PATH` имеет приоритет (deployment-specific override).
_BUNDLED_YAML_PATH = Path(__file__).parent / "owner_aliases.yaml"


_FILE_CACHE: Optional[Dict[str, str]] = None
_FILE_CACHE_PATH: Optional[str] = None
_BUNDLED_CACHE: Optional[Dict[str, str]] = None


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


def _get_bundled_aliases() -> Dict[str, str]:
    """Прочитать bundled YAML рядом с модулем. Кэшируется in-process.

    Файл отсутствует → пустой dict (test environments / minimal install)."""
    global _BUNDLED_CACHE
    if _BUNDLED_CACHE is not None:
        return _BUNDLED_CACHE
    if _BUNDLED_YAML_PATH.exists():
        _BUNDLED_CACHE = _load_yaml_aliases(str(_BUNDLED_YAML_PATH))
    else:
        _BUNDLED_CACHE = {}
    return _BUNDLED_CACHE


def get_aliases() -> Dict[str, str]:
    """Вернуть объединённый маппинг username (lower) → team string (с @).

    Приоритет (более поздние оверрайдят ранние):
      1. `_DEFAULT_ALIASES` — hardcoded safety net.
      2. Bundled `owner_aliases.yaml` рядом с модулем.
      3. ENV `OWNER_ALIASES_PATH` — deployment override.

    Кэшируем по пути файла — повторные вызовы не читают disk каждый раз.
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
    # 1. hardcoded defaults
    for k, v in _DEFAULT_ALIASES.items():
        merged[k.lower()] = v
    # 2. bundled YAML (overrides defaults)
    for k, v in _get_bundled_aliases().items():
        merged[k] = v
    # 3. ENV-pointed YAML (overrides bundled)
    for k, v in file_aliases.items():
        merged[k] = v
    return merged


def is_known_username(username: str) -> bool:
    """True если username имеет alias-маппинг (в YAML или дефолтах).

    Используется в `ownership_suggester._deploy_history_top` чтобы отличить
    «реальный owner» (squad-N/platform) от fallback-а `@?-{username}`. Только
    known users контрибутят strength=N/total в сигнал B; unknown — strength=0.0.
    """
    if not username:
        return False
    return username.lower().strip() in get_aliases()


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
    """Тестовый хелпер — сбросить in-process кэш файла.

    Сбрасывает оба кэша: ENV-overrides и bundled YAML.
    """
    global _FILE_CACHE, _FILE_CACHE_PATH, _BUNDLED_CACHE
    _FILE_CACHE = None
    _FILE_CACHE_PATH = None
    _BUNDLED_CACHE = None
