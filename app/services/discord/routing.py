"""Routing helpers: severity gate, per-team webhook map, wait-param.

Без локального state — каждый вызов читает settings заново. Это
позволяет тестам monkeypatch'ить settings без перезагрузки модуля.
"""

import json
from typing import Dict, Optional

import structlog

from app.config import settings

_log = structlog.get_logger("discord.routing")

# Severity-routing (#3): какие уровни идут в #infra-error.
# critical/warning → True; info/none/empty → False (daily-digest only).
_ROUTEABLE_SEVERITIES = {"critical", "warning"}


def _should_route_to_error(severity: Optional[str]) -> bool:
    return (severity or "").strip().lower() in _ROUTEABLE_SEVERITIES


def _parse_team_channel_map() -> Dict[str, str]:
    """Распарсить DISCORD_TEAM_CHANNEL_MAP в dict.

    Не-JSON / пусто → пустой dict (silently). Логируем при ошибке парсинга —
    misconfiguration лучше видеть в логах, но не падать.
    """
    raw = settings.DISCORD_TEAM_CHANNEL_MAP
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if v}
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        _log.warning("team_channel_map_invalid", error=type(e).__name__)
    return {}


def _pick_webhook_url(
    team_owner: Optional[str],
    severity: Optional[str] = None,
) -> Optional[str]:
    """Per-team routing (#10).

    Приоритет: team_owner ∈ map → per-team url; иначе DISCORD_WEBHOOK_URL.
    severity сейчас не влияет на выбор канала, оставлен в сигнатуре для
    форвард-совместимости (например, info → отдельный канал).
    """
    if team_owner:
        team_map = _parse_team_channel_map()
        if team_map.get(team_owner):
            return team_map[team_owner]
    return settings.DISCORD_WEBHOOK_URL


def _ensure_wait_param(url: str) -> str:
    """Добавить ?wait=true к webhook URL — без этого Discord не возвращает
    message_id, и edit-цикл (#1/#9) не работает.

    Сохраняет существующие query-параметры.
    """
    if not url:
        return url
    if "wait=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}wait=true"
