"""Backward-compatibility shim — see app/services/discord/.

14 existing import sites (app/api/webhooks.py, app/celery_worker.py,
app/workers/{pipeline,tasks}.py, app/knowledge_graph/{confidence,
external_probe_sync}.py, app/services/{alert_enrichment,chronic_digest,
k8s_service,pii_redaction,stats_digest,team_digest}.py, плюс 6 тестов)
импортируют отсюда — модуль остаётся, чтобы их не править.

Шим работает через `sys.modules`-alias: `app.services.discord_service`
указывает на тот же объект что и `app.services.discord.service`. Это
важно для тестов с `unittest.mock.patch("app.services.discord_service.X")` —
патч мутирует атрибут на ровно том модуле, который читает код
DiscordService (включая `settings`, `httpx`, и методы класса).

Дополнительно сюда докидываются helpers из соседних submodule'ов
(embed_builder/dedup/routing), чтобы `from discord_service import _foo`
и `discord_service._recent_incidents.clear()` тоже работали как раньше.
"""
import sys

from app.services.discord import dedup as _dedup
from app.services.discord import embed_builder as _embed_builder
from app.services.discord import routing as _routing
from app.services.discord import service as _service_module

# Module-alias: подменяем себя на service.py. Все патчи в тестах
# (settings/httpx/DiscordService) попадают в правильный namespace.
sys.modules[__name__] = _service_module

# Перекинуть имена-helpers из соседних submodule'ов на service.py, чтобы
# `discord_service._foo` тоже работало. Только после module-alias.
_REEXPORT_FROM_DEDUP = (
    "_DEDUP_TTL_SEC", "_LINKED_MIN_COUNT", "_LINKED_WINDOW_SEC",
    "_dedup_lock", "_purge_dedup_state", "_recent_by_alertname",
    "_recent_incidents", "_webhook_edit_endpoint",
)
_REEXPORT_FROM_EMBED_BUILDER = (
    "_build_deploy_correlation_field", "_build_log_error_rate_field",
    "_format_recurrence_tag", "_format_sha_link",
    "_summarize_self_health_detail",
)
_REEXPORT_FROM_ROUTING = (
    "_ROUTEABLE_SEVERITIES", "_ensure_wait_param",
    "_parse_team_channel_map", "_pick_webhook_url", "_should_route_to_error",
)
for _name in _REEXPORT_FROM_DEDUP:
    setattr(_service_module, _name, getattr(_dedup, _name))
for _name in _REEXPORT_FROM_EMBED_BUILDER:
    setattr(_service_module, _name, getattr(_embed_builder, _name))
for _name in _REEXPORT_FROM_ROUTING:
    setattr(_service_module, _name, getattr(_routing, _name))
