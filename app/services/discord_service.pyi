"""Type stub for the discord_service shim.

The runtime module replaces itself via `sys.modules[__name__] = _service_module`
(see discord_service.py). mypy doesn't follow that magic, so this stub
declares the public API explicitly. Keep in sync with the .py shim's
_REEXPORT_FROM_* tuples if those change.
"""
from app.services.discord.service import DiscordService as DiscordService
from app.services.discord.service import discord_service as discord_service

from app.services.discord.dedup import (
    _DEDUP_TTL_SEC as _DEDUP_TTL_SEC,
    _LINKED_MIN_COUNT as _LINKED_MIN_COUNT,
    _LINKED_WINDOW_SEC as _LINKED_WINDOW_SEC,
    _compute_content_key as _compute_content_key,
    _dedup_lock as _dedup_lock,
    _purge_dedup_state as _purge_dedup_state,
    _recent_by_alertname as _recent_by_alertname,
    _recent_incidents as _recent_incidents,
    _webhook_edit_endpoint as _webhook_edit_endpoint,
)
from app.services.discord.embed_builder import (
    _build_deploy_correlation_field as _build_deploy_correlation_field,
    _build_log_error_rate_field as _build_log_error_rate_field,
    _format_recurrence_tag as _format_recurrence_tag,
    _format_sha_link as _format_sha_link,
    _summarize_self_health_detail as _summarize_self_health_detail,
)
from app.services.discord.routing import (
    _ROUTEABLE_SEVERITIES as _ROUTEABLE_SEVERITIES,
    _ensure_wait_param as _ensure_wait_param,
    _parse_team_channel_map as _parse_team_channel_map,
    _pick_webhook_url as _pick_webhook_url,
    _should_route_to_error as _should_route_to_error,
)
