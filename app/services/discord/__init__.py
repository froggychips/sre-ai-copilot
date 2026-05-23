"""Discord-отправка: класс DiscordService + тематические helpers.

Структура:
  * service.py        — DiscordService class (HTTP sending, embed assembly)
  * embed_builder.py  — field builders (suspect deploy, log error rate, …)
  * dedup.py          — module-level state + webhook PATCH endpoint helper
  * routing.py        — team-channel map, severity gate, wait-param helper

Backward-compat shim `app/services/discord_service.py` делает
`from app.services.discord import *` и переиздаёт все эти имена под
старым путём импорта — никакие из 14 импортирующих файлов не правились.
"""

from .dedup import (
    _DEDUP_TTL_SEC,
    _LINKED_MIN_COUNT,
    _LINKED_WINDOW_SEC,
    _compute_content_key,
    _dedup_lock,
    _purge_dedup_state,
    _recent_by_alertname,
    _recent_incidents,
    _webhook_edit_endpoint,
)
from .embed_builder import (
    _build_deploy_correlation_field,
    _build_log_error_rate_field,
    _format_recurrence_tag,
    _format_sha_link,
    _summarize_self_health_detail,
)
from .routing import (
    _ROUTEABLE_SEVERITIES,
    _ensure_wait_param,
    _parse_team_channel_map,
    _pick_webhook_url,
    _should_route_to_error,
)
from .service import DiscordService, discord_service

__all__ = [
    # Класс и инстанс — основной публичный API.
    "DiscordService",
    "discord_service",
    # Routing / severity gate.
    "_should_route_to_error",
    "_parse_team_channel_map",
    "_pick_webhook_url",
    "_ensure_wait_param",
    "_ROUTEABLE_SEVERITIES",
    # Embed builders — тестовый API (test_discord_incident_wave3).
    "_format_sha_link",
    "_format_recurrence_tag",
    "_build_deploy_correlation_field",
    "_build_log_error_rate_field",
    "_summarize_self_health_detail",
    # Dedup state + endpoint helper — тестовый API (clear() кэшей).
    "_recent_incidents",
    "_recent_by_alertname",
    "_dedup_lock",
    "_purge_dedup_state",
    "_webhook_edit_endpoint",
    "_compute_content_key",
    "_DEDUP_TTL_SEC",
    "_LINKED_WINDOW_SEC",
    "_LINKED_MIN_COUNT",
]
