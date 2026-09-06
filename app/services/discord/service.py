"""DiscordService — основная логика отправки embed-ов в Discord.

Сюда переехала вся отправка (webhook + bot API), POST/PATCH dedup-цикл,
и сборка крупных embed-ов (incident report, enriched alert, external probe
alert, self-health alert). Helpers вынесены в соседние модули:
  * embed_builder — поля для embed-ов (suspect deploy, log error rate, ...)
  * routing       — выбор канала по team_owner, severity gate
  * dedup         — module-level state + helper для PATCH-endpoint

Класс не трогаем — split первой волны касается только free-function helpers.
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import httpx
import structlog

from app.config import settings

from . import dedup as _dedup_state
from . import dedup_store
from .dedup import (
    _DEDUP_TTL_SEC,
    _LINKED_MIN_COUNT,
    _LINKED_WINDOW_SEC,
    _compute_content_key,
    _compute_enriched_key,
    _dedup_lock,
    _incident_dedup_key,
    _purge_dedup_state,
    _webhook_edit_endpoint,
)
from .embed_builder import (
    SEVERITY_COLOR_RESOLVED,
    _age_decay_severity,
    _build_blast_radius_field,
    _build_deploy_correlation_field,
    _build_ingress_health_field,
    _build_log_error_rate_field,
    _build_nats_impact_field,
    _build_pod_trail_field,
    _build_runbook_field,
    _build_similar_past_field,
    _build_tldr_field,
    _decay_color,
    _format_recurrence_tag,
    _format_sha_link,
    _lookup_similar_past_incident_cached,
    _allowed_mentions,
    _mention_block,
    _self_health_footer,
    _severity_to_color,
    _summarize_self_health_detail,
    _tc_build_url,
)
from .routing import _ensure_wait_param, _pick_webhook_url, _should_route_to_error
from app.utils.time_human import humanize_minutes_ago

# structlog для DRY_RUN-логов — стандартный python `logging` отфильтровывается
# на корневом WARNING level в production, поэтому [DISCORD_DRY_RUN] раньше
# не появлялись в kubectl logs. structlog идёт через тот же sink что и
# kg.populate.done / enrich_forward.suppress_chronic — visibility гарантирована.
_dry_run_log = structlog.get_logger("discord.dry_run")
_log = structlog.get_logger("discord")

if TYPE_CHECKING:
    from app.core.execution_dsl import ExecutionIntent
    from app.services.alert_enrichment import EnrichedContext

# Discord embed colour codes
_COLOR_CRITICAL = 0xE53935   # red
_COLOR_WARNING  = 0xFDD835   # yellow
_COLOR_RESOLVED = 0x43A047   # green
_COLOR_UNKNOWN  = 0x9E9E9E   # grey
# Suppressed (silenced/inhibited) — orange, чтобы on-call видел «есть состояние,
# но не red». Embed всё ещё уходит, но визуально снижен приоритет.
_COLOR_SUPPRESSED = 0xFB8C00  # orange

_SEVERITY_COLORS = {
    "critical": _COLOR_CRITICAL,
    "warning":  _COLOR_WARNING,
    "info":     _COLOR_UNKNOWN,
}

# ── #3: Discord rate-limit (HTTP 429) retry-параметры ────────────────────────
# Alert-storm генерит 429 (Discord global/route rate-limit). Bounded-retry:
# максимум _RATELIMIT_MAX_ATTEMPTS попыток и _RATELIMIT_MAX_TOTAL_WAIT секунд
# суммарного сна — иначе send-путь мог бы залипнуть на злом Retry-After.
_RATELIMIT_MAX_ATTEMPTS = 3
_RATELIMIT_MAX_TOTAL_WAIT = 10.0
_RATELIMIT_DEFAULT_WAIT = 1.0

# ── #6: Discord 6000-char TOTAL embed limit ──────────────────────────────────
# Суммарная длина embed-а = title + description + все field.name + field.value +
# footer.text + author.name. Полностью обогащённый critical (до ~24 полей) может
# это превысить → Discord 400 → alert дропается. Держим safe-margin ниже лимита.
_EMBED_TOTAL_LIMIT = 6000
_EMBED_SAFE_MARGIN = 5900

# Поштучные лимиты Discord. Суммарный 6000 их НЕ подменяет: embed на 3000
# символов из 30 полей отвергается ровно так же, как embed на 7000 из трёх, и
# ответ приходит один и тот же — 400 с телом вида
# `embeds.0.fields: BASE_TYPE_MAX_LENGTH`.
#
# Учитывался только один из них и только в team-digest'е («не больше 25 полей
# на embed — лимит Discord»), а enriched-путь, где полей набирается ровно 25
# `fields.append`, не проверял ничего кроме суммы.
#
# Пустое `value` — отдельный случай той же природы: Discord считает поле с
# пустым name или value невалидным (BASE_TYPE_REQUIRED), а собирается такое
# поле само собой, из `"\n".join(lines)` по пустому списку.
_EMBED_MAX_FIELDS = 25
_EMBED_MAX_TITLE = 256
_EMBED_MAX_DESCRIPTION = 4096
_EMBED_MAX_FOOTER = 2048
_EMBED_MAX_AUTHOR = 256
_EMBED_MAX_FIELD_NAME = 256
_EMBED_MAX_FIELD_VALUE = 1024

# Поля-«излишества» enrichment-а в порядке дропа: СНАЧАЛА самые нижние по
# приоритету (pod-trail/ingress/similar-past/blast-radius), потом остальное.
# Root-cause («🎯 Скорее всего»), TL;DR, Namespaces/Owner и title/description/
# footer НЕ трогаем. Матчинг — по подстроке в field.name.
_EMBED_DROP_ORDER: Tuple[str, ...] = (
    "Pod trail",          # 🕒 Pod trail (Wave 7)
    "Endpoint health",    # 🌐 ingress-derived health
    "Similar past",       # 🔁 Similar past
    "Blast radius",       # 🎯 Blast radius (Wave 7)
    "NATS impact",        # 📨 NATS impact (Wave 7)
    "Inbound callers",    # KG inbound callers
    "Upstream",           # Upstream сейчас (KG)
    "Source",             # Prometheus generator link
    "Tickets",            # 🎫 Jira tickets
    "Recent pod events",  # k8s pod events
    "Зависит от",         # outgoing deps (critical)
    "Deps",               # outgoing deps (warning compact)
    "Runbook",            # 📖 Runbook
    "Deploy-связь",       # NS-scope deploy verdict
    "Recent deploys",     # service-scope deploys
    "Почему важно",       # ⭐ why-this-matters
)

# Поля, которые никогда не дропаем и не режем (root-cause + header essentials).
_EMBED_PROTECTED_FIELDS: Tuple[str, ...] = (
    "🎯 Скорее всего",  # primary_hypothesis — root cause
    "🎯 TL;DR",         # header TL;DR
    "Namespaces",
    "Owner",
    # Для нодового алерта имя ноды — это и есть ответ «где», без него
    # остаётся только IP пода-экспортёра. Дропу не подлежит.
    "Нода",
)


# ── stats-дайджест: обрезка description с сохранением критичного хвоста ──────
# Discord embed.description hard-limit 4096, держим safe-margin 4000.
_STATS_DESC_LIMIT = 4000

# Блоки дайджеста, которые обрезка НЕ имеет права съесть. Дайджест из ~20
# секций регулярно перерастает лимит, а слепой `description[:3990]` рубил
# ровно хвост — то есть сначала предупреждение «Секции недоступны» (ради
# которого механизм самодиагностики и делался после инцидента 07.08.2026),
# затем kg_quality и footer с heartbeat'ами синков. Дайджест терял именно те
# строки, по которым читатель понимает, можно ли верить остальным.
#
# Матчинг по подстроке — маркеры совпадают с тем, что рендерит stats_digest
# (`section_failures_line`, `kg_quality_section`, `beat_heartbeats_footer`).
# Импортировать оттуда константы намеренно не стали: discord-сервис не должен
# зависеть от модуля дайджеста (тот сам импортит discord_service).
_STATS_PROTECTED_MARKERS: Tuple[str, ...] = (
    "Секции недоступны",   # самодиагностика сборки
    "🧬 KG quality",       # состояние графа
    "_Syncs:",             # footer beat-heartbeats
)
_STATS_CUT_MARKER = "_…truncated_"
_STATS_DROP_MARKER = "_…вырезано секций: {n} (лимит Discord)…_"
# Меньше этого куска обрезанный блок бессмыслен — дропаем целиком.
_STATS_MIN_PARTIAL_BLOCK = 120


def _truncate_stats_description(
    description: str, limit: int = _STATS_DESC_LIMIT
) -> str:
    """Ужать digest под лимит, сохранив критичные блоки и порядок чтения.

    Режем ПО СЕКЦИЯМ (блоки, разделённые пустой строкой), а не по символам:
      1. защищённые блоки (`_STATS_PROTECTED_MARKERS`) откладываем целиком, и
         те, что стояли ДО первой обычной секции (строка «Секции
         недоступны»), остаются на своём месте — сверху, как их и читают;
      2. остальную голову набираем по порядку, пока хватает бюджета; первый
         не влезший блок при осмысленном остатке режем с `_…truncated_`;
      3. количество выброшенных секций проговариваем строкой — «дайджест
         короче обычного» не должно выглядеть как «в кластере тихо».
    """
    if len(description) <= limit:
        return description

    sep = "\n\n"
    blocks = description.split(sep)
    protected_idx = [
        i for i, b in enumerate(blocks)
        if any(m in b for m in _STATS_PROTECTED_MARKERS)
    ]
    protected_set = set(protected_idx)
    head_idx = [i for i in range(len(blocks)) if i not in protected_set]
    first_head = head_idx[0] if head_idx else len(blocks)

    lead = sep.join(blocks[i] for i in protected_idx if i < first_head)
    tail = sep.join(blocks[i] for i in protected_idx if i >= first_head)
    head = [blocks[i] for i in head_idx]

    # Патологический случай: сами защищённые блоки не влезают в лимит. Тогда
    # приоритизировать уже нечего — режем по символам, как раньше.
    protected_text = sep.join(p for p in (lead, tail) if p)
    if len(protected_text) + len(sep) + len(_STATS_CUT_MARKER) > limit:
        return (
            protected_text[: limit - len(_STATS_CUT_MARKER) - 1]
            + "\n" + _STATS_CUT_MARKER
        )

    reserve = len(protected_text) + (len(sep) if protected_text else 0)
    reserve += len(sep) + len(_STATS_DROP_MARKER.format(n=len(head)))
    budget = limit - reserve

    kept: List[str] = []
    used = 0
    dropped = 0
    for i, block in enumerate(head):
        extra = len(sep) if kept else 0
        if used + extra + len(block) <= budget:
            kept.append(block)
            used += extra + len(block)
            continue
        room = budget - used - extra - len(sep) - len(_STATS_CUT_MARKER)
        if room >= _STATS_MIN_PARTIAL_BLOCK:
            kept.append(block[:room] + "\n" + _STATS_CUT_MARKER)
            dropped = len(head) - i - 1
        else:
            dropped = len(head) - i
        break

    parts = [p for p in (lead, sep.join(kept)) if p]
    if dropped:
        parts.append(_STATS_DROP_MARKER.format(n=dropped))
    if tail:
        parts.append(tail)
    return sep.join(parts)


def _parse_retry_after(resp: Any, default: float = _RATELIMIT_DEFAULT_WAIT) -> float:
    """Сколько ждать до ретрая по Discord 429 (в секундах).

    Приоритет: JSON body `retry_after` (Discord отдаёт float-секунды), затем
    header `Retry-After`. При отсутствии/парс-ошибке — `default`.
    """
    # JSON body (Discord-specific, секунды, может быть float).
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("retry_after") is not None:
            val = float(body["retry_after"])
            if val >= 0:
                return val
    except (ValueError, TypeError, AttributeError):
        pass
    # Header fallback.
    try:
        hdr = resp.headers.get("Retry-After")
        if hdr is not None:
            val = float(hdr)
            if val >= 0:
                return val
    except (ValueError, TypeError, AttributeError):
        pass
    return default


def _redact_webhook_url(url: str) -> str:
    """Webhook-URL для логов — БЕЗ токена.

    Раньше логировался `url[:60]`: у discord-вебхука
    (`https://discord.com/api/webhooks/{id}/{token}`) id заканчивается
    примерно на 52-й позиции, т.е. в лог утекали первые ~8 символов токена.
    Оставляем id вебхука (достаточно для атрибуции канала), токен-сегмент
    маскируем; суффикс `/messages/{msg_id}` PATCH-endpoint-а сохраняем.
    """
    return re.sub(r"(/webhooks/\d+)/[^/?]+", r"\1/***", url or "")


def _embed_total_len(embed: Dict[str, Any]) -> int:
    """Суммарная длина embed-а по правилам Discord 6000-char limit."""
    total = len(embed.get("title") or "")
    total += len(embed.get("description") or "")
    for f in embed.get("fields") or []:
        total += len(f.get("name") or "") + len(f.get("value") or "")
    total += len((embed.get("footer") or {}).get("text") or "")
    total += len((embed.get("author") or {}).get("name") or "")
    return total


def _drop_by_priority(embed: Dict[str, Any], keep_going) -> None:
    """Дропать поля-излишества по `_EMBED_DROP_ORDER`, пока `keep_going()`.

    Общий шаг для двух ограничений — суммарной длины и числа полей: порядок
    приоритетов один и тот же, отличается только условие остановки.
    """
    fields = list(embed.get("fields") or [])
    for marker in _EMBED_DROP_ORDER:
        if not keep_going():
            return
        fields = [f for f in fields if marker not in (f.get("name") or "")]
        embed["fields"] = fields


def _enforce_embed_shape(embed: Dict[str, Any]) -> Dict[str, Any]:
    """Привести embed к поштучным лимитам Discord.

    Отвечает на другой вопрос, чем `_fit_embed_to_limit`: не «влезаем ли в
    6000 суммарно», а «валиден ли каждый элемент по отдельности». Оба
    нарушения дают одинаковый 400, и до 05.09.2026 проверялось только первое.

    Порядок: сначала выбрасываем пустые поля (их не спасти обрезкой), потом
    режем длины, и только затем сокращаем число полей — сперва по
    приоритету дропа, потом отрезая хвост незащищённых.
    """
    if embed.get("title"):
        embed["title"] = embed["title"][:_EMBED_MAX_TITLE]
    if embed.get("description"):
        embed["description"] = embed["description"][:_EMBED_MAX_DESCRIPTION]
    footer = embed.get("footer")
    if isinstance(footer, dict) and footer.get("text"):
        footer["text"] = footer["text"][:_EMBED_MAX_FOOTER]
    author = embed.get("author")
    if isinstance(author, dict) and author.get("name"):
        author["name"] = author["name"][:_EMBED_MAX_AUTHOR]

    fields = [
        f for f in (embed.get("fields") or [])
        if (f.get("name") or "").strip() and (f.get("value") or "").strip()
    ]
    for f in fields:
        f["name"] = f["name"][:_EMBED_MAX_FIELD_NAME]
        f["value"] = f["value"][:_EMBED_MAX_FIELD_VALUE]
    embed["fields"] = fields

    if len(fields) > _EMBED_MAX_FIELDS:
        _drop_by_priority(
            embed, lambda: len(embed.get("fields") or []) > _EMBED_MAX_FIELDS,
        )
    fields = list(embed.get("fields") or [])
    if len(fields) > _EMBED_MAX_FIELDS:
        # Излишества кончились, а полей всё ещё больше лимита. Режем хвост
        # незащищённых: root-cause и header переживают любую обрезку.
        protected = [
            f for f in fields
            if any(p in (f.get("name") or "") for p in _EMBED_PROTECTED_FIELDS)
        ]
        rest = [f for f in fields if f not in protected]
        embed["fields"] = (protected + rest)[:_EMBED_MAX_FIELDS]
    return embed


def _fit_embed_to_limit(
    embed: Dict[str, Any],
    limit: int = _EMBED_TOTAL_LIMIT,
    margin: int = _EMBED_SAFE_MARGIN,
) -> Dict[str, Any]:
    """Ужимает embed под Discord 6000-char TOTAL limit (#6).

    Стратегия (пока не влезем в `margin`):
      1. дропаем поля-излишества по `_EMBED_DROP_ORDER` (снизу вверх по
         приоритету);
      2. усекаем `description`;
      3. в крайнем случае усекаем/удаляем самое длинное НЕзащищённое поле.
    Alert НИКОГДА не дропается целиком — title + root-cause + header остаются.
    Мутирует и возвращает тот же dict.

    Поштучные лимиты (`_enforce_embed_shape`) применяются ВСЕГДА, даже когда
    сумма укладывается в margin: короткий embed из тридцати полей Discord
    отвергает так же, как длинный.
    """
    _enforce_embed_shape(embed)
    if _embed_total_len(embed) <= margin:
        return embed

    # (1) Дропаем излишества по drop-order.
    _drop_by_priority(embed, lambda: _embed_total_len(embed) > margin)

    # (2) Усекаем description.
    if _embed_total_len(embed) > margin and embed.get("description"):
        over = _embed_total_len(embed) - margin
        desc = embed["description"]
        embed["description"] = desc[: max(0, len(desc) - over)]

    # (3) Крайний случай — режем самое длинное НЕзащищённое поле, пока не
    # уложимся в hard-limit. Защищённые поля (root-cause/header) не трогаем.
    while _embed_total_len(embed) > limit and embed.get("fields"):
        candidates = [
            (i, f)
            for i, f in enumerate(embed["fields"])
            if not any(p in (f.get("name") or "") for p in _EMBED_PROTECTED_FIELDS)
        ]
        if not candidates:
            break  # остались только защищённые — они заведомо < limit
        i, f = max(candidates, key=lambda t: len(t[1].get("value") or ""))
        over = _embed_total_len(embed) - margin
        val = f.get("value") or ""
        if over >= len(val):
            embed["fields"].pop(i)
        else:
            f["value"] = val[: len(val) - over]
    return embed


def _collect_self_health_summary() -> Optional[Dict[str, Any]]:
    """Best-effort: собрать сжатый snapshot self-health для footer (B6).

    Возвращает dict с ключами `kg_sync_lag_min`, `alerts_resolve_status`,
    `owner_coverage_pct`. None если что-то fail-нулось (footer тогда
    рендерится без self-mon суффиксов — embed уходит без задержки).

    Без I/O в hot-path enriched alert: одна short PG-сессия с LIMIT-чтениями.
    """
    try:
        from app.database import SessionLocal
        from app.knowledge_graph.self_health import (
            check_alerts_resolve_freshness,
            check_sync_lag,
        )
        from app.knowledge_graph.schema import Service
        from sqlalchemy import func

        db = SessionLocal()
        try:
            summary: Dict[str, Any] = {}
            # KG sync lag — берём freshest task; считаем lag минимальный
            # (главное — есть ли хоть один свежий sync, а не максимум).
            sync_res = check_sync_lag(db)
            per_task = (sync_res.detail or {}).get("per_task") or {}
            min_lag: Optional[float] = None
            for task_info in per_task.values():
                lag = task_info.get("lag_minutes")
                if lag is None:
                    continue
                try:
                    lag_f = float(lag)
                except (ValueError, TypeError):
                    continue
                if min_lag is None or lag_f < min_lag:
                    min_lag = lag_f
            if min_lag is not None:
                summary["kg_sync_lag_min"] = min_lag

            # alerts_resolve freshness — ok/warn/fail.
            ar_res = check_alerts_resolve_freshness(db)
            summary["alerts_resolve_status"] = ar_res.status

            # Owner coverage — share of services с non-null team_owner.
            total = db.query(func.count(Service.id)).scalar() or 0
            if total > 0:
                owned = (
                    db.query(func.count(Service.id))
                    .filter(Service.team_owner.isnot(None))
                    .filter(Service.team_owner != "")
                    .scalar()
                    or 0
                )
                summary["owner_coverage_pct"] = round(100.0 * owned / total, 2)

            return summary
        finally:
            db.close()
    except Exception as e:
        _log.warning("self_health_footer_summary_failed", error=type(e).__name__)
        return None


def _render_compact_warning_line(
    *,
    severity: str,
    alertname: str,
    service_or_pod: str,
    duration_label: Optional[str],
    team_owner: Optional[str],
) -> str:
    """B12 — однострочный warning embed для compact_mode=warning_only.

    Формат:
        🟡 KubeStatefulSetReplicasMismatch · clickhouse-keeper · 46.3h · @infra
    """
    icon = {"warning": "🟡", "critical": "🔴", "resolved": "✅"}.get(severity.lower(), "⚪")
    parts = [f"{icon} {alertname}", service_or_pod]
    if duration_label:
        parts.append(duration_label)
    if team_owner:
        parts.append(f"@{team_owner}")
    return " · ".join(parts)


class DiscordService:
    async def send_report(self, report_text: str):
        if settings.DISCORD_DRY_RUN:
            _dry_run_log.info("discord.dry_run.send_report", text=report_text[:500])
            return
        url = settings.DISCORD_WEBHOOK_URL
        if not url:
            logging.warning("DISCORD_WEBHOOK_URL not set, skipping send_report")
            return
        payload = {"content": report_text}
        async with httpx.AsyncClient() as client:
            # #3: через ratelimit-обёртку — голый POST в alert-storm ловил 429
            # и молча терял сообщение (Retry-After не читался).
            await self._request_with_ratelimit(client, "post", url, json=payload)

    async def send_stats_report(self, content: str) -> bool:
        """Отправить markdown-content в канал #stats как Discord embed.

        Используем embed (description-limit 4096) вместо content (limit 2000).
        Daily-digest сейчас ~2800 chars — в content не влезет.

        Первая строка контента вынесена в embed.title (если bold + emoji),
        остальное — в description; переполнение режется по секциям с
        сохранением критичных строк (`_truncate_stats_description`).

        Возвращает фактический статус доставки — по нему stats_digest решает,
        писать ли deadman-маркер (раньше недоставка молча глоталась и маркер
        писался при мёртвом вебхуке):
          * True  — POST прошёл (2xx) либо DISCORD_DRY_RUN (доставка
            подавлена намеренно);
          * False — вебхук не настроен или Discord ответил >=400.
        """
        if settings.DISCORD_DRY_RUN:
            _dry_run_log.info("discord.dry_run.send_stats_report", content=content[:500])
            return True
        url = settings.DISCORD_WEBHOOK_STATS_URL
        if not url:
            logging.warning("DISCORD_WEBHOOK_STATS_URL not set, skipping stats report")
            return False

        lines = content.split("\n", 1)
        if len(lines) == 2 and lines[0].strip():
            title = lines[0].strip()[:256]  # Discord title-limit
            description = lines[1].lstrip("\n")
        else:
            title = "Stats digest"
            description = content
        # Embed description hard-limit 4096. Обрезаем по секциям с сохранением
        # критичного хвоста (самодиагностика / kg_quality / heartbeats) —
        # слепой срез по символам съедал именно их (см.
        # `_truncate_stats_description`).
        description = _truncate_stats_description(description)

        payload = {
            "embeds": [{
                "title": title,
                "description": description,
                "color": 0x607D8B,  # blue-grey, нейтральный для аналитики
            }]
        }
        async with httpx.AsyncClient() as client:
            # #3: 429 в alert-storm ретраится (bounded), а не глотается.
            r = await self._request_with_ratelimit(client, "post", url, json=payload)
            if r.status_code >= 400:
                logging.error(
                    "discord_stats_report_failed",
                    extra={"status": r.status_code, "body": r.text[:200]},
                )
                return False
        return True

    async def send_incident_report(
        self,
        incident_id: str,
        alertname: str,
        namespace: str,
        pod: Optional[str],
        service: Optional[str],
        node: Optional[str],
        severity: str,
        cause: Optional[str],
        resolution_quality: str,
        synthesis: str,
        is_recurrence: bool = False,
        flap_count: int = 0,
        execution_intent: Optional["ExecutionIntent"] = None,
        executor_result: Optional[dict] = None,
        deploy_correlation: Optional[Dict[str, Any]] = None,
        team_owner: Optional[str] = None,
        recurrence_count_24h: int = 0,
        recurrence_count_7d: int = 0,
        incident_ts: Optional[datetime] = None,
        pod_event_reason: Optional[str] = None,
        metric_source: Optional[str] = None,
        fired_at: Optional[datetime] = None,
        acked_by: Optional[str] = None,
    ) -> bool:
        """Единый embed-отчёт, заменяющий сырой алерт от Spidey Bot.

        Формат: заголовок алерта (что видел Spidey Bot) + root cause +
        краткий вывод пайплайна — всё в одном Discord-сообщении.

        Контракт с workers/pipeline: возвращает bool фактической доставки и
        НИКОГДА не бросает наружу ошибку отправки (HTTP-ошибки/исключения
        send-пути глотаются с логом — pipeline полагается на это):
          * True  — embed в канале есть (POST 2xx, PATCH-dedup существующего
            сообщения, отправка через bot API или DISCORD_DRY_RUN);
          * False — доставки не было (severity-gate skip, вебхук не настроен,
            HTTP >=400 или исключение на POST-е).

        Новые kwargs (Wave 3):
          - deploy_correlation: результат `correlate_deploy_to_incident`. Если
            verdict in {likely, suspect, weak} — добавляем отдельный embed-field
            (🔴/🟠/🟡 Suspect Deploy) с confidence-скором.
          - team_owner: для per-team channel routing через DISCORD_TEAM_CHANNEL_MAP.
          - recurrence_count_24h / _7d: для footer-метки «×N in 24h · M in 7d».
          - incident_ts: момент инцидента — для запроса kg_log_observations
            и dedup-cache TTL.
          - pod_event_reason: нормализованный k8s reason (OOMKilled, BackOff…)
            для content-based dedup. Если None — fallback на lowercase
            alertname.
          - metric_source: имя metric_source когда service_name не резолвится
            (синтетический алерт). Используется для content-key чтобы
            разные «безымянные» алерты не схлопывались.
        """
        # #3 severity-routing: info/none/empty → НЕ шлём в #infra-error.
        # kg_alerts всё равно содержит запись (alertmanager_sync), digest
        # её увидит. Дополнительно фильтр в дополнение к AM-маршрутизации
        # — defense-in-depth.
        if not _should_route_to_error(severity):
            _log.info(
                "incident.skipped_low_severity",
                incident_id=incident_id, severity=severity,
            )
            return False

        # A2 severity decay: critical >24h без ack → orange + 🪦 STALE prefix.
        # Helper покрывает все edge-cases (acked / younger / non-critical).
        # Используем fired_at если он передан, иначе fallback на incident_ts.
        decay_basis = fired_at or incident_ts
        decayed_sev, stale_title_prefix, stale_footer_marker = _age_decay_severity(
            severity=severity,
            fired_at=decay_basis,
            acked_by=acked_by,
        )
        is_stale_critical = decayed_sev == "stale_critical"

        base_color = (
            _COLOR_RESOLVED if resolution_quality == "resolved"
            else _SEVERITY_COLORS.get(severity.lower(), _COLOR_UNKNOWN)
        )
        # Stale-critical перекрашиваем в orange ТОЛЬКО когда инцидент не
        # помечен как resolved (resolved уже отдельный зелёный кейс).
        if resolution_quality != "resolved":
            color = _decay_color(base_color, decayed_sev)
        else:
            color = base_color

        if resolution_quality == "resolved":
            status_icon = "✅"
        elif is_stale_critical:
            # 🪦 STALE заменяет дефолтный 🚨/⚠️ префикс — оператор сразу
            # видит «висит без ack 24h+», а не «новый critical».
            status_icon = stale_title_prefix
        else:
            status_icon = "⚠️"
        # #13: recurrence label с окном (24h/7d). Fallback на старый
        # "🔁 RECURRENCE" если counts не пробросили (тесты, ручной запуск).
        recurrence_tag = _format_recurrence_tag(
            is_recurrence, recurrence_count_24h, recurrence_count_7d,
        )
        flap_tag = f" · 🔄 ×{flap_count}" if flap_count > 0 else ""
        ns_part = f" · {namespace}" if namespace else ""
        title = f"{status_icon} {alertname}{ns_part}{recurrence_tag}{flap_tag}"
        # Discord title-limit 256: длинный alertname+ns раньше давал 400 и
        # алерт дропался целиком. Маркер обрезки — как в enriched-пути.
        if len(title) > 256:
            title = title[:255] + "…"

        fields = []
        if service:
            fields.append({"name": "Service", "value": f"`{service}`", "inline": True})
        if pod:
            fields.append({"name": "Pod", "value": f"`{pod}`", "inline": True})
        if node and not pod:
            # Node-level alerts (Node* family) don't have pod/namespace context
            fields.append({"name": "Node", "value": f"`{node}`", "inline": True})
        fields.append({
            "name": "Root Cause",
            "value": (cause or "Manual triage required — no hypothesis survived")[:1024],
            "inline": False,
        })

        # #2: Suspect deploy block. Если verdict != suspect — поле не добавляем.
        suspect_field = _build_deploy_correlation_field(deploy_correlation or {})
        if suspect_field is not None:
            fields.append(suspect_field)

        # B4: Similar past incident lookup. Best-effort, без LLM.
        # Skip-if-not-applicable: нет alertname/service/namespace → не делаем
        # DB lookup. resolved-инциденту тоже не показываем (поле "что было
        # раньше с той же проблемой" нужно только для firing).
        if (
            resolution_quality != "resolved"
            and alertname
            and service
            and namespace
        ):
            try:
                similar = await _lookup_similar_past_incident_cached(
                    alertname=alertname,
                    service_name=service,
                    namespace=namespace,
                )
                similar_field = _build_similar_past_field(similar)
                if similar_field is not None:
                    fields.append(similar_field)
            except Exception as e:
                # Best-effort: embed уходит без поля.
                logging.debug("similar_past_lookup_skipped: %s", type(e).__name__)

        # #8: Log error rate ±10min. Best-effort — пропускается тихо, если
        # service_id не резолвится или kg_log_observations пуст.
        if incident_ts is not None:
            log_field = _build_log_error_rate_field(service, namespace, incident_ts)
            if log_field is not None:
                fields.append(log_field)
        # PR #1 executor track: показываем структурированный proposed action,
        # если FixAgent сумел выдать ExecutionIntent. Пока ничего НЕ выполняется
        # (advisory-mode), это просто визуальный сигнал.
        if execution_intent is not None:
            from app.core.execution_dsl import DSLTranslator
            try:
                kubectl_cmd = DSLTranslator.to_kubectl(execution_intent)
            except Exception:
                kubectl_cmd = "(translation failed)"
            risk_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(
                execution_intent.risk.lower(), "⚪"
            )
            fields.append({
                "name": f"{risk_emoji} Proposed action (advisory)",
                "value": (
                    f"`{kubectl_cmd}`\n"
                    f"_risk: {execution_intent.risk} · "
                    f"action: {execution_intent.action.value}_"
                )[:1024],
                "inline": False,
            })
        # PR #2 executor track: показываем результат server-side dry-run.
        # Это ВЕРИФИКАЦИЯ команды через kube-apiserver, не запуск (см. EXECUTOR_ENABLED).
        if executor_result is not None and executor_result.get("status") != "skipped":
            status_map = {
                "dry_run_ok":         ("✓",  "dry-run OK (kube-apiserver валидировал)"),
                "dry_run_failed":     ("✗",  "dry-run failed"),
                "guardrail_blocked":  ("🚫", "K8sSecurityGuard заблокировал"),
                "error":              ("⚠️", "executor exception"),
            }
            icon, label = status_map.get(
                executor_result.get("status", ""), ("?", executor_result.get("status", "unknown"))
            )
            detail = (
                executor_result.get("stderr")
                or executor_result.get("reason")
                or executor_result.get("error")
                or executor_result.get("stdout")
                or ""
            )
            value = f"{icon} {label}"
            if detail:
                value += f"\n```\n{detail[:600]}\n```"
            fields.append({
                "name": "Dry-run verdict",
                "value": value[:1024],
                "inline": False,
            })

        # Synthesis truncated — Discord limit 4096, но читаемость важнее.
        description = synthesis[:1200] + ("…" if len(synthesis) > 1200 else "")

        # Базовые feedback-кнопки. Кнопка "Apply" появляется только когда
        # EXECUTOR_APPROVAL_ENABLED + intent распарсен + dry-run ok + risk low/medium
        # (см. PR #3 executor track). HIGH-risk и любая дисквалификация — manual.
        action_row: list = [
            {
                "type": 2, "style": 3,  # BUTTON SUCCESS (green)
                "label": "👍 Верный анализ",
                "custom_id": f"feedback_pos_{incident_id}",
            },
            {
                "type": 2, "style": 4,  # BUTTON DANGER (red)
                "label": "👎 Анализ неверен",
                "custom_id": f"feedback_neg_{incident_id}",
            },
        ]
        # Подпись intent-а нужна и для apply-кнопки (TOCTOU на apply-пути), и
        # для approve/decline. Считаем один раз.
        intent_sig: Optional[str] = None
        if execution_intent is not None:
            from app.services.intent_signature import compute_signature
            intent_sig = compute_signature(execution_intent)

        if (
            settings.EXECUTOR_APPROVAL_ENABLED
            and execution_intent is not None
            and execution_intent.risk.lower() in {"low", "medium"}
            and executor_result is not None
            and executor_result.get("status") == "dry_run_ok"
        ):
            action_row.append({
                "type": 2, "style": 1,  # BUTTON PRIMARY (blurple)
                "label": "⚙️ Apply (kubectl)",
                "custom_id": f"apply:{incident_id}:{intent_sig}",
            })

        # Approve/Decline кнопки для proposed action (PR #12 executor track).
        # Появляются ТОЛЬКО при EXECUTOR_APPROVAL_ENABLED (prod opt-in на
        # реальный write — как у ⚙️ Apply выше; approve-кнопка ведёт к тому же
        # apply_intent и не должна обходить флаг) и когда есть execution_intent.
        # Шлём через bot API; на webhook-пути buttons не работают (Discord
        # ограничение), поэтому второй row добавляется только когда
        # _can_send_via_bot()==True.
        approve_row: Optional[list] = None
        if (
            settings.EXECUTOR_APPROVAL_ENABLED
            and execution_intent is not None
            and self._can_send_via_bot()
        ):
            sig = intent_sig
            approve_row = [
                {
                    "type": 2, "style": 3,  # SUCCESS (green)
                    "label": "Approve & Run",
                    "custom_id": f"approve:{incident_id}:{sig}",
                },
                {
                    "type": 2, "style": 4,  # DANGER (red)
                    "label": "Decline",
                    "custom_id": f"decline:{incident_id}:{sig}",
                },
            ]

        components: list = [{"type": 1, "components": action_row}]
        if approve_row:
            components.append({"type": 1, "components": approve_row})

        # A2: декей-маркер в футере, если инцидент stale-critical (>24h без ack).
        footer_text = f"incident/{incident_id}"
        if is_stale_critical and stale_footer_marker:
            footer_text = f"{footer_text} · {stale_footer_marker}"
        embed: Dict[str, Any] = {
            "title": title,
            "color": color,
            "fields": fields,
            "description": description,
            "footer": {"text": footer_text[:2048]},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        # #6: тот же 6000-char TOTAL guard, что и в enriched-пути. Полностью
        # обогащённый инцидент (root cause 1024 + suspect deploy + similar past
        # + proposed action + dry-run verdict) превышал лимит → Discord 400 →
        # алерт дропался целиком.
        _fit_embed_to_limit(embed)
        payload = {
            "embeds": [embed],
            "components": components,
        }

        if settings.DISCORD_DRY_RUN:
            _dry_run_log.info(
                "discord.dry_run.send_incident_report",
                title=title, cause=cause, resolution_quality=resolution_quality,
                via_bot=bool(approve_row),
                team_owner=team_owner,
                deploy_suspect=(deploy_correlation or {}).get("verdict") in ("likely", "suspect"),
            )
            return True

        # Если есть approve/decline buttons — обязаны слать через bot API
        # (webhook не рендерит interactive components). Иначе fallback на
        # webhook как раньше. Per-team routing + dedup в bot-path не делаем —
        # bot-канал фиксирован настройкой.
        if approve_row and self._can_send_via_bot():
            sent = await self._send_via_bot(payload)
            if sent:
                return True
            # Bot-send упал — fallback на webhook, но БЕЗ approve-кнопок
            # (Discord webhook отвергнет любые custom_id-components кроме
            # тех что от того же application — у webhook'а нет application_id).
            payload["components"] = [{"type": 1, "components": action_row}]
            logging.warning("discord_incident_bot_send_failed_fallback_to_webhook")

        # #10 per-team routing. Резолв webhook через team_owner; fallback на
        # DISCORD_WEBHOOK_URL.
        url = _pick_webhook_url(team_owner=team_owner, severity=severity)
        if not url:
            logging.warning("DISCORD_WEBHOOK_URL not set, skipping incident report")
            return False

        # #1 + #9 dedup. Ключи кэша:
        #   - content-key (alertname, ns, service, reason) — точечный дедуп
        #     (#1). Раньше был AM fingerprint через (alertname, ns, pod),
        #     но AM минтит свежий fingerprint при каждой ре-mission →
        #     dedup не срабатывал. Content-key решает.
        #   - per (alertname,) — burst-агрегация (#9), если ≥3 за 5 мин.
        # Берём решение под локом.
        return await self._post_or_edit_incident(
            url=url,
            payload=payload,
            embed=embed,
            alertname=alertname,
            namespace=namespace or "",
            pod=pod or "",
            service=service,
            severity=severity,
            pod_event_reason=pod_event_reason,
            metric_source=metric_source,
        )

    async def _post_or_edit_incident(
        self,
        url: str,
        payload: Dict[str, Any],
        embed: Dict[str, Any],
        alertname: str,
        namespace: str,
        pod: str,
        service: Optional[str],
        severity: str,
        pod_event_reason: Optional[str] = None,
        metric_source: Optional[str] = None,
    ) -> bool:
        """Решает: новый POST или PATCH существующего сообщения.

        Возвращает bool доставки (контракт send_incident_report): True — embed
        в канале есть (свежий POST 2xx либо dedup-ветка: сообщение уже
        запощено/PATCH-ится, дубль подавлен намеренно), False — свежий POST
        не удался (HTTP >=400 / исключение). Наружу не бросает.

        Логика:
          1. Если content-key (alertname, ns, service, reason) уже в кэше
             <30 мин — PATCH embed (увеличиваем count, обновляем footer
             last_seen). Это основной dedup-путь.
             Если content-key resolve фейлит (нет service) — fallback на
             старый AM-style key (alertname, ns, pod).
          2. Иначе если (alertname,) видели ≥_LINKED_MIN_COUNT раз за
             _LINKED_WINDOW_SEC — PATCH первое сообщение этого alertname,
             добавляем pod/ns в виде fields/footer.
          3. Иначе — POST новый embed с ?wait=true (чтобы получить msg_id).

        Если webhook не возвращает message_id (wait=false) — fallback на POST
        без записи в кэш.
        """
        now = time.time()
        # Content-based key (новый). Fallback на (alertname,ns,pod) если
        # service не резолвится.
        content_key = _compute_content_key(
            alertname=alertname,
            namespace=namespace,
            service_name=service,
            reason=pod_event_reason,
            metric_source=metric_source,
        )
        if content_key is not None:
            key_full = content_key
            dedup_mode = "content"
        else:
            # Fallback: legacy key для случаев когда service не резолвится
            # (hard-to-route alerts). Тоже строка для единого типа.
            key_full = f"fingerprint:{alertname}:{namespace}:{pod}"
            dedup_mode = "fingerprint"
            logging.debug(
                "fallback to fingerprint dedup: alertname=%s ns=%s pod=%s",
                alertname, namespace, pod,
            )
        key_alert = (alertname,)
        # Cross-replica dedup-key (PG-store discord_dedup, как enriched-канал).
        pg_key = _incident_dedup_key(key_full)

        # #1: PATCH сообщения если content-key (или fallback) уже в store.
        # claim() ходит в Postgres (fallback на in-memory при недоступном PG):
        # per-process dict ломался на 2 репликах api — каждый под промахивался
        # мимо чужого кэша и дублировал critical-POST с mention.
        # Атомарный claim-before-post: раньше get_fresh → POST → save давал
        # TOCTOU-окно, в котором обе реплики промахивались и обе постили
        # (дубль-@here). Теперь ключ клеймится ДО POST-а (INSERT-конфликт по
        # PK); проигравший видит запись и PATCH-ит либо молча выходит.
        existing_full = dedup_store.claim(
            pg_key, ttl_sec=_DEDUP_TTL_SEC, now=now,
            alertname=alertname, namespace=namespace,
            service=service, severity=severity,
        )
        if existing_full is not None:
            if not existing_full.get("msg_id"):
                # Placeholder другой реплики (mid-post) либо legacy-POST без
                # msg_id — PATCH-ить нечего, дубль не шлём.
                logging.info(
                    "discord_incident_dedup_claimed_elsewhere key=%s", key_full,
                )
                # Сообщение постит другая реплика — дедуп, не потеря.
                return True
            self._audit_dedup_event(
                "DEDUP_HIT_CONTENT" if dedup_mode == "content"
                else "DEDUP_HIT_FINGERPRINT",
                alertname=alertname, namespace=namespace,
                service=service, key=key_full,
            )
            await self._patch_recurrence_exact(url, embed, pg_key, now)
            # Исходное сообщение в канале живо — фейл PATCH-а (только счётчик
            # recurrence) недоставкой не считаем.
            return True

        # Свежий — записываем DEDUP_MISS_FRESH ниже после успешного POST.
        self._audit_dedup_event(
            "DEDUP_MISS_FRESH",
            alertname=alertname, namespace=namespace,
            service=service, key=key_full, dedup_mode=dedup_mode,
        )

        # #9: burst-aggregation по alertname остаётся per-process (вторичный
        # путь, не источник дубль-mention: ведёт к PATCH, не к новому POST).
        with _dedup_lock:
            _purge_dedup_state(now)
            existing_alert = _dedup_state._recent_by_alertname.get(key_alert)
        if (
            existing_alert is not None
            and (now - existing_alert.get("first_ts", 0)) <= _LINKED_WINDOW_SEC
            and existing_alert.get("count", 1) >= _LINKED_MIN_COUNT - 1
        ):
            # Идём linked-PATCH-веткой (pg-store она не использует) —
            # отпускаем claim, чтобы не глушить последующие content-key hits.
            dedup_store.release(pg_key)
            await self._patch_recurrence(
                url, embed, key_full, key_alert, namespace, pod,
                mode="linked", now=now,
            )
            # Burst-агрегация: первое сообщение alertname уже в канале.
            return True

        # Иначе — новый POST (claim наш). wait=true чтобы получить msg_id
        # для будущего edit.
        post_url = _ensure_wait_param(url)
        msg_id: Optional[str] = None
        try:
            async with httpx.AsyncClient() as client:
                r = await self._request_with_ratelimit(
                    client, "post", post_url, json=payload
                )
                if r.status_code >= 400:
                    logging.error(
                        "discord_incident_report_failed",
                        extra={"status": r.status_code, "body": r.text[:200]},
                    )
                    # POST не случился — отпускаем claim, иначе ключ молча
                    # глушит алерты до конца TTL-окна.
                    dedup_store.release(pg_key)
                    return False
                # wait=true → 200 OK + JSON message. wait=false → 204 No Content.
                if r.status_code == 200:
                    try:
                        msg_id = str(r.json().get("id") or "") or None
                    except (ValueError, TypeError):
                        msg_id = None
        except Exception as e:
            logging.error("discord_incident_report_exception", extra={"error": str(e)})
            dedup_store.release(pg_key)
            return False

        if not msg_id:
            # Без msg_id мы не сможем PATCH-ить (legacy webhook, wait=false) —
            # контракт прежний: dedup-state не пополняем, следующий incident
            # того же ключа пойдёт новым POST-ом. Claim отпускаем, иначе
            # placeholder глушил бы его до конца TTL-окна.
            dedup_store.release(pg_key)
            # POST при этом прошёл (2xx без тела) — доставка состоялась.
            return True

        # Cross-replica: фиксируем POST в PG-store (UPSERT по pg_key).
        # webhook_url в store НЕ кладём — это токен на постинг в канал, а
        # PATCH-у он и не нужен: `url` резолвится из настроек тем же
        # _pick_webhook_url в момент PATCH-а.
        dedup_store.save(
            pg_key,
            msg_id=msg_id,
            embed=embed,
            alertname=alertname,
            namespace=namespace,
            service=service,
            severity=severity,
            now=now,
        )

        with _dedup_lock:
            # Burst-агрегация: разделяем счётчик по alertname (per-process).
            existing_alert = _dedup_state._recent_by_alertname.get(key_alert)
            if existing_alert and (now - existing_alert.get("first_ts", 0)) <= _LINKED_WINDOW_SEC:
                existing_alert["count"] = existing_alert.get("count", 1) + 1
                existing_alert["last_ts"] = now
            else:
                _dedup_state._recent_by_alertname[key_alert] = {
                    "msg_id": msg_id,
                    "first_ts": now,
                    "last_ts": now,
                    "count": 1,
                    # webhook_url тут тоже не держим (симметрично с PG-store):
                    # незачем размазывать токен по heap-у, PATCH резолвит его
                    # из настроек.
                    "embed": embed,
                    "group_ns_pod": [f"{namespace}/{pod}"],
                }
        return True

    async def _patch_recurrence_exact(
        self,
        url: str,
        embed: Dict[str, Any],
        pg_key: str,
        now: float,
    ) -> None:
        """PATCH content-key recurrence через cross-replica dedup_store.

        count++ атомарен в PG (dedup_store.bump) — закрывает дубль-mention на
        2+ репликах. Footer формата `<base> · ×N в 30мин · first .. last ..`
        (как раньше у in-memory exact-пути).
        """
        rec = dedup_store.bump(pg_key, now=now)
        if rec is None:
            # Запись исчезла между get_fresh и bump (purge/race) — выходим.
            return
        msg_id = rec["msg_id"]
        cached_embed = rec.get("embed") or embed
        count = rec["count"]
        first_ts = rec["first_ts"]
        # PATCH-endpoint собираем из `url` — того же, что резолвил
        # _pick_webhook_url перед POST-ом. В store его больше нет (токен
        # в БД = право спамить в канал с любого read-only доступа).
        webhook_url = url

        patched_embed = dict(cached_embed)
        first_seen = datetime.fromtimestamp(first_ts, tz=timezone.utc).strftime("%H:%M")
        last_seen = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%H:%M")
        original_footer = (patched_embed.get("footer") or {}).get("text") or ""
        base_footer = original_footer.split(" · ", 1)[0] if original_footer else ""
        patched_embed["footer"] = {
            "text": (
                f"{base_footer} · ×{count} в 30мин · "
                f"first {first_seen} · last {last_seen}"
            )[:2048]
        }
        patch_payload = {"embeds": [patched_embed]}

        endpoint = _webhook_edit_endpoint(webhook_url, msg_id)
        if not endpoint:
            logging.warning("discord_patch_no_endpoint", extra={"webhook": webhook_url[:40]})
            return
        try:
            async with httpx.AsyncClient() as client:
                r = await self._request_with_ratelimit(
                    client, "patch", endpoint, json=patch_payload
                )
                if r.status_code >= 400:
                    logging.warning(
                        "discord_incident_patch_failed",
                        extra={"status": r.status_code, "body": r.text[:200]},
                    )
                    return
        except Exception as e:
            logging.warning("discord_incident_patch_exception", extra={"error": str(e)})
            return
        # Кэшируем обновлённый embed (footer-история) в store.
        dedup_store.update_embed(pg_key, patched_embed)

    async def _patch_recurrence(
        self,
        url: str,
        embed: Dict[str, Any],
        key_full: str,
        key_alert: Tuple[str],
        namespace: str,
        pod: str,
        mode: str,
        now: float,
    ) -> None:
        """PATCH ранее отправленного embed: bump count, обновить footer.

        mode="exact" — тот же (alertname,ns,pod), считаем occurrences.
        mode="linked" — burst-аггрегация по alertname; добавляем pod/ns в group.
        """
        with _dedup_lock:
            if mode == "exact":
                rec = _dedup_state._recent_incidents.get(key_full)
            else:
                rec = _dedup_state._recent_by_alertname.get(key_alert)
            if rec is None:
                return  # race: запись только что протухла
            rec["count"] = rec.get("count", 1) + 1
            rec["last_ts"] = now
            if mode == "linked":
                group = rec.setdefault("group_ns_pod", [])
                marker = f"{namespace}/{pod}"
                if marker not in group:
                    group.append(marker)
            msg_id = rec["msg_id"]
            cached_embed = rec.get("embed") or embed
            count = rec["count"]
            first_ts = rec["first_ts"]
            group = rec.get("group_ns_pod") or []
            # Как и в exact-пути: URL берём из аргумента, а не из кэша.
            webhook_url = url

        # Обновляем footer и (для linked) добавляем поле с группой ns/pod.
        patched_embed = dict(cached_embed)
        first_seen = datetime.fromtimestamp(first_ts, tz=timezone.utc).strftime("%H:%M")
        last_seen = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%H:%M")
        original_footer = (patched_embed.get("footer") or {}).get("text") or ""
        # Извлекаем incident/<id> кусок если есть.
        base_footer = original_footer.split(" · ", 1)[0] if original_footer else ""
        patched_embed["footer"] = {
            "text": (
                f"{base_footer} · ×{count} в 30мин · "
                f"first {first_seen} · last {last_seen}"
            )[:2048]
        }
        if mode == "linked" and group:
            new_fields = list(patched_embed.get("fields") or [])
            # Заменяем / добавляем поле «Affected pods».
            existing_idx = None
            for i, f in enumerate(new_fields):
                if f.get("name") == "Affected pods":
                    existing_idx = i
                    break
            value = ", ".join(f"`{x}`" for x in group[:8])
            if len(group) > 8:
                value += f" (+{len(group) - 8})"
            field = {"name": "Affected pods", "value": value[:1024], "inline": False}
            if existing_idx is not None:
                new_fields[existing_idx] = field
            else:
                new_fields.append(field)
            patched_embed["fields"] = new_fields
        patch_payload = {"embeds": [patched_embed]}

        endpoint = _webhook_edit_endpoint(webhook_url, msg_id)
        if not endpoint:
            logging.warning("discord_patch_no_endpoint", extra={"webhook": webhook_url[:40]})
            return
        try:
            async with httpx.AsyncClient() as client:
                r = await self._request_with_ratelimit(
                    client, "patch", endpoint, json=patch_payload
                )
                if r.status_code >= 400:
                    logging.warning(
                        "discord_incident_patch_failed",
                        extra={"status": r.status_code, "body": r.text[:200]},
                    )
                    return
        except Exception as e:
            logging.warning("discord_incident_patch_exception", extra={"error": str(e)})
            return
        # Кэшируем обновлённый embed (чтобы следующий patch не терял fields).
        with _dedup_lock:
            if mode == "exact" and key_full in _dedup_state._recent_incidents:
                _dedup_state._recent_incidents[key_full]["embed"] = patched_embed
            if mode == "linked" and key_alert in _dedup_state._recent_by_alertname:
                _dedup_state._recent_by_alertname[key_alert]["embed"] = patched_embed

    def _audit_dedup_event(
        self,
        event_type: str,
        *,
        alertname: str,
        namespace: str,
        service: Optional[str],
        key: str,
        dedup_mode: Optional[str] = None,
    ) -> None:
        """Структурированная audit-запись dedup-решения.

        Без числовых counter'ов — обычные log lines в audit_logger, чтобы
        в Loki/ELK можно было `count by event_type` и быстро увидеть
        долю HIT_CONTENT vs HIT_FINGERPRINT vs MISS_FRESH.
        """
        try:
            from app.services.audit_logger import audit_logger
            audit_logger.info(
                event_type,
                event_type=event_type,
                alertname=alertname,
                namespace=namespace,
                service=service,
                dedup_key=key,
                dedup_mode=dedup_mode,
            )
        except Exception as e:  # never let telemetry break the send-path
            logging.debug("audit_dedup_event_failed: %s", e)

    async def _request_with_ratelimit(
        self,
        client: "httpx.AsyncClient",
        method: str,
        url: str,
        *,
        json: Dict[str, Any],
        max_attempts: int = _RATELIMIT_MAX_ATTEMPTS,
        max_total_wait: float = _RATELIMIT_MAX_TOTAL_WAIT,
    ) -> "httpx.Response":
        """POST/PATCH с bounded-retry на Discord rate-limit (HTTP 429) (#3).

        Раньше все send-пути трактовали status >= 400 как терминальный → 429
        (что и генерит alert-storm) дропался, `Retry-After` не читался, msg_id
        в dedup_store не сохранялся → следующий идентичный alert ре-POSTил →
        снова 429 (loop). Теперь на 429 читаем `Retry-After` (header или JSON
        body `retry_after`, секунды, может быть float), спим и ретраим ТОТ ЖЕ
        запрос. Кап: `max_attempts` попыток и `max_total_wait` суммарного сна.

        Non-429 ответы (включая прочие 4xx/5xx) возвращаются как есть — их
        обрабатывает вызывающий (log + return), поведение не меняется.
        """
        send = client.post if method == "post" else client.patch
        resp = await send(url, json=json)
        attempts = 1
        total_waited = 0.0
        while (
            resp.status_code == 429
            and attempts < max_attempts
            and total_waited < max_total_wait
        ):
            retry_after = _parse_retry_after(resp)
            wait = min(retry_after, max_total_wait - total_waited)
            if wait <= 0:
                break
            logging.warning(
                "discord_rate_limited",
                extra={
                    "retry_after": retry_after,
                    "attempt": attempts,
                    # Без токен-сегмента: url[:60] раньше утаскивал в логи
                    # первые ~8 символов webhook-токена.
                    "url": _redact_webhook_url(url)[:80],
                },
            )
            await asyncio.sleep(wait)
            total_waited += wait
            attempts += 1
            resp = await send(url, json=json)
        return resp

    def _can_send_via_bot(self) -> bool:
        """Bot API доступен, когда есть token + channel_id. Без них fallback на webhook."""
        return bool(
            getattr(settings, "DISCORD_BOT_TOKEN", None)
            and getattr(settings, "DISCORD_INCIDENT_CHANNEL_ID", None)
        )

    async def _send_via_bot(self, payload: Dict[str, Any]) -> bool:
        """POST через bot API в incident-channel. Возвращает True при успехе.

        В отличие от webhook, bot API поддерживает interactive components
        (buttons). Используется для embed-ов с approve/decline кнопками.
        """
        token = settings.DISCORD_BOT_TOKEN
        channel_id = settings.DISCORD_INCIDENT_CHANNEL_ID
        if not token or not channel_id:
            return False
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        headers = {
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(url, headers=headers, json=payload)
                if r.status_code >= 400:
                    logging.error(
                        "discord_bot_send_failed",
                        extra={"status": r.status_code, "body": r.text[:300]},
                    )
                    return False
                return True
        except Exception as e:
            logging.error("discord_bot_send_exception", extra={"error": str(e)})
            return False

    async def send_enriched_alert(
        self,
        contexts: List["EnrichedContext"],
        env: Optional[str] = None,
        resurfaced: bool = False,
    ) -> bool:
        """Детерминированный embed с KG-контекстом, БЕЗ LLM.

        Принимает batch `EnrichedContext` (несколько алертов одного типа в
        одном AM-batch'е сворачиваются в один embed). Старый Spidey Bot
        слал по сообщению на каждый алерт; здесь один embed на logical-group.

        Не вызывает модель. Latency бюджет — <500ms p95 синхронно
        в HTTP-handler'е /webhooks/alertmanager/enrich-and-forward.

        Возвращает delivered — тот же контракт, что у send_incident_report /
        send_stats_report: True когда embed в канале есть (POST 2xx,
        PATCH-dedup, placeholder живой реплики, DISCORD_DRY_RUN), False когда
        доставки не было (severity-gate, нет вебхука, HTTP>=400, исключение).
        Наружу не бросает. Нужно вызывающей стороне в
        /webhooks/alertmanager/enrich-and-forward: без явного False откат
        tentative-инкремента chronic-счётчика срабатывал только на
        исключениях, а HTTP>=400 молча оставлял счётчик наращённым.
        """
        if not contexts:
            return False
        head = contexts[0]
        incident = head.incident
        labels = incident.labels
        alertname = labels.get("alertname", "unknown")
        severity = (incident.severity or "unknown").lower()

        # #3 severity-routing: info/none → не шлём в #infra-error.
        if not _should_route_to_error(severity):
            _log.info(
                "incident.skipped_low_severity",
                alertname=alertname, severity=severity, path="enriched",
            )
            return False

        # Цвет + emoji. B-блок #11 — severity-tier visual codes
        # (red/yellow/green/orange, title prefix 🚨/⚠️/✅/🔁).
        # Старая `_SEVERITY_COLORS` map оставлена для обратной совместимости
        # incident-path; enriched alert использует новые tier-цвета.
        color = _severity_to_color(severity, resurfaced=resurfaced)
        # Muted-классы (grey + 🔇, без 🚨/@mention): rollout-normal, meta-агрегат
        # и gen-mismatch-churn (KubeDeploymentGenerationMismatch при ready==desired).
        muted_noise = head.rollout_noise or head.meta_noise or head.gen_mismatch_noise
        if muted_noise:
            color = _COLOR_UNKNOWN
        # A1: suppressed (silenced/inhibited) — orange override, чтобы on-call
        # сразу видел «AM уже знает что заглушено». Имеет приоритет над
        # severity-color, но не над muted-noise (все = grey).
        if head.inhibition_state and not muted_noise:
            color = _COLOR_SUPPRESSED
        # Title prefix: новые severity-aware emoji (🚨/⚠️/✅/🔁). Fallback на
        # старый `🔴/🟡/⚪` для unknown. Suppressed состояние имеет свой icon (🟠).
        # meta/gen-mismatch-noise — 🔇, приоритет над 🚨: даёт on-call сразу
        # понять «это плумбинг-шум / доброкачественный churn, не пожар».
        if head.meta_noise or head.gen_mismatch_noise:
            icon = "🔇"
        elif head.inhibition_state and not head.rollout_noise:
            icon = "🟠"
        elif resurfaced:
            icon = "🔁"
        else:
            icon = {"critical": "🚨", "warning": "⚠️"}.get(severity, "⚪")
        env_part = f"{env.upper()} · " if env else ""

        # namespace список (если несколько алертов одного типа в разных ns)
        namespaces = []
        seen_ns = set()
        for c in contexts:
            ns = c.incident.namespace or c.incident.labels.get("namespace") or "?"
            if ns not in seen_ns:
                seen_ns.add(ns)
                namespaces.append(ns)

        ns_str = ", ".join(namespaces[:4]) + (f" (+{len(namespaces) - 4})" if len(namespaces) > 4 else "")
        # У нодовых алертов сервиса нет, а pod — это `vm-node-exporter-XXXXX`,
        # то есть источник метрики, а не то, что сломалось. Имя ноды (его
        # резолвит node_resolver по метке pod) занимает место сервиса: в
        # заголовке должно стоять `dev-14`, а не IP пода и не «vm-node».
        svc_or_pod = head.service or head.node or head.pod or "?"

        recurrence_tag = ""
        rec_max = max((len(c.recurrence_24h) for c in contexts), default=0)
        if rec_max >= 2:
            recurrence_tag = f" · 🔁 ×{rec_max} за 24h"
        if head.meta_noise:
            noise_tag = " · 🔇 META-AGGREGATE"
        elif head.gen_mismatch_noise:
            noise_tag = " · 🔇 GENERATION-CHURN"
        elif head.rollout_noise:
            noise_tag = " · 🤖 ROLLOUT-NORMAL"
        else:
            noise_tag = ""
        ns_tag = f" ({len(namespaces)} ns)" if len(namespaces) > 1 else ""
        resurfaced_tag = " · 🌀 RESURFACED" if resurfaced else ""

        title = (
            f"{icon} {env_part}{alertname} · {svc_or_pod}{ns_tag}"
            f"{recurrence_tag}{noise_tag}{resurfaced_tag}"
        )

        # Phase 3-A: severity-aware enrichment depth.
        # Warning embed'ов в 5-10× больше чем critical → noise сосредоточен
        # там. Для warning показываем «minimum viable» — title + owner +
        # most-likely-cause + why-this-matters. Для critical — полный embed.
        is_critical = severity == "critical"

        fields: List[Dict[str, Any]] = []

        # B1 — TL;DR first-line (heuristic-driven). Сразу после header,
        # до всех остальных полей. Если ни одна heuristic не сработала и
        # incident.description пустой — поле пропускается.
        tldr_field = _build_tldr_field(
            summary=incident.description or incident.summary,
            pod_events=head.pod_events,
            # NS-scope deploys в TL;DR не передаём: его heuristics
            # формулируют «deploy сервиса → регресс», что для деплоев
            # соседей по ns было бы ложной атрибуцией. NS-вердикт
            # рендерится своим полем «Deploy-связь».
            recent_deploys=(
                head.recent_deploys
                if getattr(head, "deploy_scope", "service") == "service"
                else []
            ),
            replicas_ready_desired=head.replicas_ready_desired,
            recurrence_24h=head.recurrence_24h,
            chronic_count=rec_max,
        )
        if tldr_field:
            fields.append(tldr_field)

        fields.append({
            "name": "Namespaces",
            "value": f"`{ns_str}`",
            "inline": True,
        })
        # Нода отдельным полем, а не только в заголовке: у нодовых алертов это
        # единственная координата «где именно», а заголовок сворачивается.
        if head.node:
            fields.append({
                "name": "Нода",
                "value": f"`{head.node}`",
                "inline": True,
            })
        if head.team_owner:
            fields.append({
                "name": "Owner",
                "value": f"`{head.team_owner}`",
                "inline": True,
            })
        if not head.in_kg:
            fields.append({
                "name": "KG",
                "value": "_сервис не в graph — topology unknown_",
                "inline": True,
            })

        # A1: AM inhibit/silence state. Если в AM payload пришло
        # `status: {state: suppressed, silencedBy/inhibitedBy: [...]}` —
        # показываем on-call что alert уже залушен и кем. Цвет embed-а уже
        # переключен на orange выше (см. inhibition_state-блок color-override).
        if head.inhibition_state:
            fields.append({
                "name": "Status",
                "value": head.inhibition_state[:512],
                "inline": False,
            })

        # On-call UX polish (10:38 feedback). Три inline-поля компактным
        # рядком: какая реплика жива, какой именно pod, и почему container
        # упал. `skip-if-empty` — поля append-аются только если данные есть.
        if head.replicas_ready_desired:
            fields.append({
                "name": "Replicas",
                "value": f"`{head.replicas_ready_desired}`",
                "inline": True,
            })
        if head.pod_name:
            fields.append({
                "name": "Pod",
                "value": f"`{head.pod_name}`",
                "inline": True,
            })
        if head.container_reason:
            fields.append({
                "name": "Reason",
                "value": f"`{head.container_reason}`",
                "inline": True,
            })

        # Phase 3-A: Most likely cause — deterministic, top-1 rule fact.
        # Это превращает embed из «вот данные» в «вот ответ». Главная
        # ценность для скорости triage.
        hyp_text = head.primary_hypothesis()
        if hyp_text:
            fields.append({
                "name": "🎯 Скорее всего",
                "value": hyp_text[:1024],
                "inline": False,
            })

        # Phase 3-A: Why this matters — derived signals (shared dep, chronic,
        # recurrence). Прирост priorization без LLM, из существующих данных.
        matter_bullets = head.why_this_matters()
        if matter_bullets:
            fields.append({
                "name": "⭐ Почему важно",
                "value": "\n".join(matter_bullets)[:1024],
                "inline": False,
            })

        # B3 — Runbook link. Pure-dict map alertname → anchor. URL prefix —
        # env override `RUNBOOK_URL_PREFIX`. Если для alertname нет matched
        # anchor — field не добавляется (skip-if-empty).
        runbook_field = _build_runbook_field(
            alertname=alertname,
            url_prefix=getattr(settings, "RUNBOOK_URL_PREFIX", "") or "",
        )
        if runbook_field:
            fields.append(runbook_field)

        # Recent deploys. Окно вычисляется из самого дальнего deploy —
        # alert_enrichment может использовать fallback 7д если узкое окно
        # пусто; заголовок честно показывает фактический диапазон.
        # Phase 3-A: для warning — top-1 строка, для critical — full 3.
        # NS-scope deploys рендерятся отдельным полем «Deploy-связь» ниже.
        if head.recent_deploys and getattr(head, "deploy_scope", "service") == "service":
            lines = []
            max_min = 0
            top_n = 3 if is_critical else 1
            for d in head.recent_deploys[:top_n]:
                mins = d.get("minutes_before_incident", 0)
                try:
                    max_min = max(max_min, int(mins))
                except (ValueError, TypeError):
                    pass
                sha_full = d.get("sha") or ""
                repo = d.get("repo")
                num = d.get("number") or "?"
                bt_name = d.get("buildtype_name") or d.get("buildtype_id") or "?"
                status = d.get("status") or ""
                triggered = d.get("triggered_by") or ""
                # Sub-task: clickable TC build URLs.
                # _tc_build_url пробует extras.url → build_id+TC_URL_PREFIX.
                # Если ни того ни другого — fallback на plain ID label.
                tc_prefix = getattr(settings, "TC_URL_PREFIX", "") or getattr(settings, "TEAMCITY_WEB_URL", "") or ""
                url = _tc_build_url(
                    build_url=d.get("url"),
                    build_id=d.get("build_id") or d.get("id"),
                    tc_url_prefix=tc_prefix,
                )
                by_part = f" by `{triggered}`" if triggered else ""
                # Build label — кликабельный если есть TC URL.
                if url:
                    # «Build and update #2138 by wizaryx» — full человекочитаемый
                    # лейбл. by_part интегрирован в линк, не как отдельный suffix.
                    if triggered:
                        build_label = f"[{bt_name} #{num} by {triggered}]({url})"
                        by_part = ""  # уже в линке
                    else:
                        build_label = f"[{bt_name} #{num}]({url})"
                else:
                    build_label = f"`#{num}` ({bt_name})"
                # #7 sha-link: markdown-ссылка на gitlab если репо известно.
                sha_link = _format_sha_link(sha_full, repo) if sha_full else ""
                sha_part = f" {sha_link}" if sha_link else ""
                status_part = f" — {status}" if status else ""
                when = humanize_minutes_ago(mins)
                lines.append(
                    f"• {build_label}{by_part}{sha_part} — {when}{status_part}"
                )
            # Человекочитаемая шкала окна: «60м» / «24ч» / «3д».
            if max_min < 120:
                window_label = f"~{max_min}м"
            elif max_min < 60 * 48:
                window_label = f"~{max_min // 60}ч"
            else:
                window_label = f"~{max_min // (60 * 24)}д"
            fields.append({
                "name": f"Recent deploys ({window_label})",
                "value": "\n".join(lines)[:1024],
                "inline": False,
            })
        elif getattr(head, "deploy_scope", "service") == "service":
            # Evidence-контракт: деплоев не показали — но «не было» или «не
            # проверено»? Если RecentDeployRule ответил ? (kg_deployments
            # упал, поток не пополняется, сервиса нет в графе), молчание
            # embed-а читается как «деплоя не было». Говорим явно.
            unknown_deploy = [
                f for f in (getattr(head, "rule_facts", None) or [])
                if getattr(f, "kind", None) == "recent_deploy"
                and getattr(f, "verdict", "") == "unknown"
            ]
            if unknown_deploy:
                reason = unknown_deploy[0].unknown_reason or "источник недоступен"
                fields.append({
                    "name": "❔ Deploy-связь не проверена",
                    "value": (
                        f"_{reason}. Не путать с «деплоя не было» — связь с "
                        f"деплоем не установлена ни в одну сторону. Проверь "
                        f"деплои в TeamCity вручную._"
                    )[:1024],
                    "inline": False,
                })

        # NS-level deploy attribution (запрос on-call 2026-06-10): для
        # алертов без резолва сервиса отвечаем «деплой или нет» деплоями
        # всего namespace. Негативный вердикт не менее ценен позитивного —
        # «деплоев не было» сразу отсекает ветку triage.
        ns_scope_ctxs = [
            c for c in contexts
            if getattr(c, "deploy_scope", "service") == "namespace"
        ]
        # Statics-aware restart attribution (инцидент 2026-07-02): недавний
        # bump статики env'а объясняет волну self-restart'ов (k8s Deployment
        # не менялся) — приоритетнее cross-ns collateral и без @mention. Берём
        # самый свежий bump среди ns-scope контекстов группы.
        statics_bump: Dict[str, Any] = {}
        for c in ns_scope_ctxs:
            b = getattr(c, "statics_bump", None) or {}
            if b and (
                not statics_bump
                or b.get("minutes_before", 1_000_000) < statics_bump.get("minutes_before", 1_000_000)
            ):
                statics_bump = b
        # Здоровье источника деплоев (инцидент 2026-08-11). Если хоть один
        # ns-scope контекст сообщил stale — вердикт «деплоя не было» запрещён:
        # мы не знаем. Берём самый свежий (минимальный age) снимок, чтобы
        # multi-ns embed не объявил поток мёртвым по одному отстающему ns.
        deploy_stream: Dict[str, Any] = {}

        def _stream_age(snapshot: Dict[str, Any]) -> float:
            """age_hours как число. None (пустая таблица) — «бесконечно старо»."""
            v = snapshot.get("age_hours")
            return float(v) if isinstance(v, (int, float)) else float("inf")

        for c in ns_scope_ctxs:
            s = getattr(c, "deploy_stream", None) or {}
            if not s:
                continue
            if not deploy_stream or _stream_age(s) < _stream_age(deploy_stream):
                deploy_stream = s
        deploy_stream_stale = bool(deploy_stream.get("stale"))
        # Минуты до алерта у ближайшего ns-scope деплоя — вход для
        # mention-подавления ниже (deploy-related critical не пингует).
        ns_deploy_min_minutes: Optional[int] = None
        if ns_scope_ctxs:
            window_min = max(
                (c.ns_deploy_window_min or 0) for c in ns_scope_ctxs
            ) or int(getattr(settings, "ENRICH_DEPLOY_LOOKBACK_MIN", 60))
            # Merge по группе (multi-ns embed = несколько контекстов),
            # dedupe по (buildtype, number) — один TC-билд деплоит чанк
            # сервисов одного ns и попал бы в поле десятком строк.
            merged: List[Dict[str, Any]] = []
            seen_builds = set()
            for c in ns_scope_ctxs:
                for d in c.recent_deploys:
                    bkey = (d.get("buildtype_id"), d.get("number"))
                    if bkey in seen_builds:
                        continue
                    seen_builds.add(bkey)
                    merged.append(d)
            merged.sort(key=lambda d: d.get("minutes_before_incident") or 0)
            ns_label = ", ".join(f"`{n}`" for n in namespaces[:4])
            if merged:
                # None в minutes (деплой в окне, но время не вычислилось) —
                # консервативно 0: лучше лишний раз не пингануть, чем пинг
                # на заведомо deploy-related волну.
                ns_deploy_min_minutes = min(
                    int(d.get("minutes_before_incident") or 0) for d in merged
                )
                dep_lines = []
                for d in merged[:3]:
                    bt_name = d.get("buildtype_name") or d.get("buildtype_id") or "?"
                    num = d.get("number") or "?"
                    triggered = d.get("triggered_by") or ""
                    by_part = f" by `{triggered}`" if triggered else ""
                    mins = d.get("minutes_before_incident", "?")
                    d_ns = d.get("namespace") or "?"
                    dep_lines.append(
                        f"• {bt_name} #{num}{by_part} — {mins}м до алерта · `{d_ns}`"
                    )
                value = (
                    "🚀 _Возможно связано с деплоем:_\n" + "\n".join(dep_lines)
                )
                if (
                    is_critical
                    and getattr(settings, "DISCORD_SUPPRESS_MENTION_ON_DEPLOY", True)
                    and ns_deploy_min_minutes
                    <= int(getattr(settings, "DISCORD_MENTION_SUPPRESS_DEPLOY_WINDOW_MIN", 30))
                ):
                    value += "\n🔕 _Mention подавлен — алерт связан с деплоем._"
            else:
                # В ns алерта деплоя нет. Прежде чем выдать негативный
                # вердикт — проверяем cross-namespace collateral: bulk-
                # rollout в соседних ns того же кластера (инцидент
                # ProdEndpointDown 2026-06-15). Берём контекст с самой
                # массовой активностью среди ns-scope контекстов группы.
                cluster_act: Dict[str, Any] = {}
                for c in ns_scope_ctxs:
                    act = getattr(c, "cluster_deploy_activity", None) or {}
                    if act.get("total_deploys", 0) > cluster_act.get("total_deploys", 0):
                        cluster_act = act
                min_deploys = int(getattr(settings, "ENRICH_CLUSTER_DEPLOY_MIN_DEPLOYS", 10))
                if statics_bump:
                    # Накат статики — приоритетный вердикт над collateral: волна
                    # рестартов = штатный self-reload статикозависимых сервисов,
                    # а не image-pull/CRI pressure от соседей.
                    ver = statics_bump.get("version")
                    prev = statics_bump.get("prev_version")
                    env_s = statics_bump.get("env") or "?"
                    mins = statics_bump.get("minutes_before", "?")
                    prev_part = f"v{prev}-{env_s}→" if prev else ""
                    value = (
                        f"📦 _Накат статики {prev_part}v{ver}-{env_s} "
                        f"({mins}м до алерта) — ожидаемый self-restart wave "
                        f"статикозависимых сервисов (town-*/map-*/bot/dev/mv/"
                        f"notificator сами рестартятся на смену хеша, k8s "
                        f"Deployment не менялся). Не cross-namespace collateral._"
                        f"\n🔕 _Mention подавлен — рестарты вызваны накатом статики._"
                    )
                elif cluster_act.get("total_deploys", 0) >= min_deploys:
                    sib_label = ", ".join(
                        f"`{n['namespace']}` ({n['deploys']})"
                        for n in cluster_act.get("namespaces", [])[:4]
                    )
                    sample_lines = []
                    for d in cluster_act.get("sample_builds", [])[:3]:
                        bt_name = d.get("buildtype_name") or d.get("buildtype_id") or "?"
                        num = d.get("number") or "?"
                        triggered = d.get("triggered_by") or ""
                        by_part = f" by `{triggered}`" if triggered else ""
                        mins = d.get("minutes_before_incident", "?")
                        d_ns = d.get("namespace") or "?"
                        sample_lines.append(
                            f"• {bt_name} #{num}{by_part} — {mins}м до алерта · `{d_ns}`"
                        )
                    value = (
                        f"⚠️ _В {ns_label} деплоя не было, но за {window_min}м рядом — "
                        f"{cluster_act['total_deploys']} деплоев в соседних ns кластера: "
                        f"{sib_label}. Возможен cross-namespace rollout-collateral "
                        f"(image-pull/CRI pressure на общих нодах)._"
                    )
                    if sample_lines:
                        value += "\n" + "\n".join(sample_lines)
                elif deploy_stream_stale:
                    # Инцидент 2026-08-11: поток деплоев стоял с 10.08, и на
                    # ProdRestartsSpike через 20 секунд после прод-раскатки
                    # embed написал «деплоев не было — вряд ли связано». Пустой
                    # kg_deployments ≠ отсутствие деплоя: пока источник молчит,
                    # честный ответ — «не знаю», иначе триаж уводится в сторону.
                    last_at = deploy_stream.get("last_at")
                    age_h = deploy_stream.get("age_hours")
                    if last_at is not None:
                        stale_part = (
                            f"последняя запись — {last_at:%d.%m %H:%M} UTC"
                            + (f" ({age_h}ч назад)" if age_h is not None else "")
                        )
                    else:
                        stale_part = "в kg_deployments нет ни одной записи"
                    value = (
                        f"❔ _Данных о деплоях нет — источник пуст: {stale_part}. "
                        f"Связь с деплоем НЕ проверена (не путать с «деплоя не "
                        f"было»): пайплайн `tc_deploys_to_kg` не пополняет KG. "
                        f"Проверь деплои в TeamCity вручную._"
                    )
                else:
                    value = (
                        f"_Деплоев в {ns_label} за {window_min}м до алерта "
                        f"не было — вряд ли связано с деплоем._"
                    )
            fields.append({
                "name": f"Deploy-связь (окно {window_min}м)",
                "value": value[:1024],
                "inline": False,
            })

        # Upstream алертит сейчас
        if head.upstream_alerts:
            lines = []
            for a in head.upstream_alerts[:5]:
                svc = a.get("service") or "?"
                ns = a.get("namespace") or "?"
                an = a.get("alertname") or "?"
                mins = a.get("minutes_before", "?")
                ek = a.get("edge_kind") or ""
                when = humanize_minutes_ago(mins) if mins != "?" else "?"
                lines.append(f"• ✗ `{svc}` @ `{ns}` — `{an}` ({when}, edge={ek})")
            fields.append({
                "name": "Upstream сейчас (KG)",
                "value": "\n".join(lines)[:1024],
                "inline": False,
            })

        # Outgoing deps — куда сервис сам ходит. Для leaf-сервисов (как
        # bot-service) это главная диагностика при падении: «упал —
        # потому что зависит от X». Группируем по kind, badge confidence.
        # Phase 3-A: для warning — counts only (одна строка), для critical — full.
        if head.outgoing_deps and is_critical:
            from app.knowledge_graph.confidence import confidence_badge

            # Phase 3-A: short provenance label из discovery_sources, чтобы
            # семантика badge была явной. `kg_sync/env_vars` → `env`,
            # `kg_sync/secret_hint` → `secret`, etc.
            def _provenance_short(srcs: list) -> str:
                if not srcs:
                    return ""
                shorts = []
                for s in srcs:
                    s_low = (s or "").lower()
                    if "secret" in s_low:
                        shorts.append("secret")
                    elif "nats" in s_low:
                        shorts.append("nats")
                    elif "url" in s_low:
                        shorts.append("url")
                    elif "env" in s_low:
                        shorts.append("env")
                    elif "dsn" in s_low:
                        shorts.append("dsn")
                    elif "runtime" in s_low:
                        shorts.append("runtime")
                    else:
                        shorts.append("?")
                return "+".join(dict.fromkeys(shorts))  # unique-preserved-order

            by_kind: Dict[str, List[str]] = {}
            for d in head.outgoing_deps:
                k = d.get("kind", "?")
                target = f"`{d.get('service','?')}`"
                target_ns = d.get("namespace") or ""
                if target_ns and target_ns != (head.incident.namespace or ""):
                    target = f"{target} @ `{target_ns}`"
                # G5: confidence-badge. ●●● multi-source+fresh → high.
                # ●○○ single-source+stale → low. LLM-pipeline (когда включится)
                # видит «inferred с confidence 0.4», а не «факт».
                score = d.get("confidence_score") or 0.0
                badge = confidence_badge(score)
                # Phase 3-A: subscript provenance — `(env+url)` рядом с badge.
                prov = _provenance_short(d.get("discovery_sources") or [])
                prov_part = f" ({prov})" if prov else ""
                target = f"{target} {badge}{prov_part}"
                by_kind.setdefault(k, []).append(target)
            lines = []
            kind_icons = {"calls": "→", "uses_db": "🗄", "uses_nats": "📡"}
            for k in sorted(by_kind):
                icon_k = kind_icons.get(k, "·")
                items = by_kind[k]
                value_str = ", ".join(items[:6])
                if len(items) > 6:
                    value_str += f" (+{len(items)-6})"
                lines.append(f"{icon_k} **{k}** ({len(items)}): {value_str}")
            fields.append({
                "name": "🔗 Зависит от · ●●●high ●●○med ●○○low",
                "value": "\n".join(lines)[:1024],
                "inline": False,
            })
        elif head.outgoing_deps and not is_critical:
            # Warning compact: counts only inline.
            by_kind_count: Dict[str, int] = {}
            for d in head.outgoing_deps:
                by_kind_count[d.get("kind", "?")] = by_kind_count.get(d.get("kind", "?"), 0) + 1
            parts = [f"{cnt} {k}" for k, cnt in by_kind_count.items()]
            fields.append({
                "name": "🔗 Deps",
                "value": " · ".join(parts) + " _(full в critical)_",
                "inline": True,
            })

        # Inbound callers — сколько сервисов вызывают этот.
        # Для high-fan-in узлов (общая БД, NATS cluster) это сигнал blast radius.
        # Phase 3-A: показываем только если sum > 5 (blast radius signal) ИЛИ critical.
        total_inbound = sum((head.inbound_count_by_kind or {}).values())
        if head.inbound_count_by_kind and (is_critical or total_inbound > 5):
            parts = [f"{cnt} через `{k}`" for k, cnt in head.inbound_count_by_kind.items()]
            fields.append({
                "name": "Inbound callers (KG)",
                "value": ", ".join(parts),
                "inline": False,
            })

        # A6: Jira tickets linkback. Тикеты project_key+label=backend
        # с service в summary за JIRA_SEARCH_DAYS. Прямые URL.
        # Phase 3-A: для warning — только если есть open тикет (priority signal).
        if head.jira_issues and (is_critical or any(j.get("status") == "open" for j in head.jira_issues)):
            lines = []
            for j in head.jira_issues[:4]:
                key = j.get("key", "?")
                summary = (j.get("summary") or "")[:80]
                status = j.get("status", "?")
                pri = j.get("priority", "")
                url = j.get("url", "")
                pri_part = f" {pri}" if pri else ""
                status_icon = {"resolved": "✅", "open": "🟡"}.get(status, "⚪")
                if url:
                    lines.append(f"• {status_icon} [`{key}`]({url}){pri_part} — {summary}")
                else:
                    lines.append(f"• {status_icon} `{key}`{pri_part} — {summary}")
            fields.append({
                "name": f"🎫 Tickets (Jira, last {settings.JIRA_SEARCH_DAYS}d)",
                "value": "\n".join(lines)[:1024],
                "inline": False,
            })

        # Recent pod_events (kg_pod_events) — k8s diagnostic signal
        # (OOMKilled / ImagePullBackOff / BackOff / Unhealthy / ...).
        # Phase 3-A: для warning — top-1 без message (most-likely-cause уже
        # выше); для critical — full top-5 с message.
        if head.pod_events:
            lines = []
            top_n = 5 if is_critical else 1
            for ev in head.pod_events[:top_n]:
                reason = ev.get("reason", "?")
                count = ev.get("count")
                mins = ev.get("minutes_before", "?")
                cnt_part = f" ×{count}" if count and count > 1 else ""
                when = humanize_minutes_ago(mins) if mins != "?" else "?"
                if is_critical:
                    msg = (ev.get("message") or "").replace("\n", " ")[:80]
                    lines.append(f"• 🩺 `{reason}`{cnt_part} — {when}: {msg}")
                else:
                    lines.append(f"• 🩺 `{reason}`{cnt_part} — {when}")
            fields.append({
                "name": "Recent pod events (k8s)",
                "value": "\n".join(lines)[:1024],
                "inline": False,
            })

        # Phase 3-A: "Гипотеза" (legacy field) удалена — теперь
        # «🎯 Скорее всего» выше по полю primary_hypothesis(). Дубликат не нужен.

        # Wave 7 (PRs #70 #71 #72): blast radius / NATS impact / pod trail.
        # Только для critical (warning compact-mode не трогаем). Каждый
        # builder сам skip-if-empty — embed не раздувается на сервисах
        # без NATS/Ingress/PodEvent данных.
        if is_critical:
            blast_field = _build_blast_radius_field(head.blast_radius)
            if blast_field:
                fields.append(blast_field)
            nats_field = _build_nats_impact_field(head.nats_impact)
            if nats_field:
                fields.append(nats_field)
            trail_field = _build_pod_trail_field(head.pod_trail)
            if trail_field:
                fields.append(trail_field)
            ingress_field = _build_ingress_health_field(head.ingress_health)
            if ingress_field:
                fields.append(ingress_field)

        # Generator link (Grafana) — если есть
        if incident.generator_url:
            fields.append({
                "name": "Source",
                "value": f"[Prometheus query]({incident.generator_url})",
                "inline": False,
            })

        description_lines = []
        if incident.description:
            description_lines.append(incident.description[:600])
        if head.rollout_noise:
            description_lines.append(
                "_Rollout в процессе (deploy <5 мин назад) — обычно безобидно._"
            )
        if head.kg_data_age_sec is not None and head.kg_data_age_sec > 2 * 3600:
            description_lines.append(
                f"_KG topology snapshot {humanize_minutes_ago(head.kg_data_age_sec // 60)} — может быть stale._"
            )
        description = "\n".join(description_lines)[:1200]

        # B6 — self-mon footer. Best-effort: один short SessionLocal-вызов,
        # exception → footer без self-mon суффиксов (embed уходит без задержки).
        base_footer = f"copilot/enrich · groupKey={(labels.get('alertname') or '?')}"
        self_health_summary = _collect_self_health_summary()
        footer_text = _self_health_footer(
            base=base_footer,
            self_health_summary=self_health_summary,
            build_version=getattr(settings, "BUILD_VERSION", "") or "",
        )

        # B12 — compact_mode. Если `warning_only` И severity=warning → одна
        # строка вместо full embed. `all` → ВСЕ embeds (включая critical) compact.
        compact_mode = (getattr(settings, "DISCORD_COMPACT_MODE", "off") or "off").lower()
        is_compact = (
            compact_mode == "all"
            or (compact_mode == "warning_only" and severity == "warning")
        )

        # B11 — `@here` mention для critical (только critical, и только когда
        # не compact-mode, чтобы compact warning не пинговал).
        #
        # Deploy-related подавление (запрос 2026-06-11, прецедент
        # PreprodRestartsSpike при деплое статики): если ближайший ns-scope
        # деплой ≤ SUPPRESS_WINDOW от алерта или rollout в процессе
        # (service-scope deploy <5 мин) — embed уходит, но без пинга.
        suppress_mention_deploy = bool(
            getattr(settings, "DISCORD_SUPPRESS_MENTION_ON_DEPLOY", True)
        ) and (
            head.rollout_noise
            or (
                ns_deploy_min_minutes is not None
                and ns_deploy_min_minutes
                <= int(getattr(settings, "DISCORD_MENTION_SUPPRESS_DEPLOY_WINDOW_MIN", 30))
            )
        )
        # meta-noise (агрегат/scrape-gap) и gen-mismatch-churn тоже без пинга —
        # карточка видима, но on-call не будят ради плумбинг-шума / churn-а.
        # Накат статики (инцидент 2026-07-02) — штатная волна self-restart'ов,
        # тоже без пинга (карточка остаётся видимой с statics-вердиктом).
        suppress_mention = (
            suppress_mention_deploy
            or head.meta_noise
            or head.gen_mismatch_noise
            or bool(statics_bump)
        )
        mention_prefix = (
            ""
            if (is_compact or suppress_mention)
            else _mention_block(severity, env)
        )
        if suppress_mention_deploy and severity == "critical":
            _log.info(
                "discord.mention_suppressed_deploy",
                alertname=alertname,
                namespaces=ns_str,
                ns_deploy_min_minutes=ns_deploy_min_minutes,
                rollout_noise=head.rollout_noise,
            )

        if is_compact:
            # Один line — без полей, без description.
            # Длительность считаем грубо: starts_at → now, если есть.
            duration_label: Optional[str] = None
            try:
                if incident.starts_at:
                    started = datetime.fromisoformat(incident.starts_at.replace("Z", "+00:00"))
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    mins = int((datetime.now(timezone.utc) - started).total_seconds() // 60)
                    if mins > 0:
                        duration_label = humanize_minutes_ago(mins).replace(" ago", "")
            except (ValueError, TypeError):
                pass
            one_line = _render_compact_warning_line(
                severity=severity,
                alertname=alertname,
                service_or_pod=svc_or_pod,
                duration_label=duration_label,
                team_owner=head.team_owner,
            )
            payload: Dict[str, Any] = {
                "content": (mention_prefix + one_line)[:2000],
                "allowed_mentions": _allowed_mentions(mention_prefix),
            }
        else:
            payload = {
                "embeds": [{
                    "title": title[:256],
                    "color": color,
                    "fields": fields,
                    "description": description,
                    "footer": {"text": footer_text},
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }],
                # Mention-payload: для critical-severity пингуем роль/`@here`
                # через `content`; allowed_mentions сужаем до роли (или
                # `everyone` для @here). Для остальных severity — empty.
                "allowed_mentions": _allowed_mentions(mention_prefix),
            }
            if mention_prefix:
                payload["content"] = mention_prefix.strip()
            # #6: держим embed под Discord 6000-char TOTAL limit. Полностью
            # обогащённый critical (до ~24 полей) может превысить → 400 → дроп.
            # Дропаем излишества enrichment-а (pod-trail/ingress/blast/...),
            # но title + root-cause + header остаются — alert не теряем.
            _fit_embed_to_limit(payload["embeds"][0])

        if settings.DISCORD_DRY_RUN:
            # Главный путь — это то место где видно фактический embed-output
            # KG-enrichment. Структурированные поля позволяют отфильтровать
            # ровно тот логи-stream в kubectl logs / VictoriaLogs.
            _dry_run_log.info(
                "discord.dry_run.send_enriched_alert",
                title=title,
                namespaces=ns_str,
                hypothesis=head.primary_hypothesis(),
                contexts_count=len(contexts),
                severity=severity,
                resurfaced=resurfaced,
                rollout_noise=head.rollout_noise,
            )
            # DRY_RUN — намеренное подавление доставки, не сбой: счётчик
            # дедупа откатывать не нужно (как в send_stats_report).
            return True
        url = settings.DISCORD_WEBHOOK_URL
        if not url:
            logging.warning("DISCORD_WEBHOOK_URL not set, skipping enriched alert")
            return False
        # Compact-mode payload не содержит embeds — PATCH-dedup-канал не
        # применим (нет structured fields для merge). Просто POST один раз.
        if is_compact:
            try:
                async with httpx.AsyncClient() as client:
                    r = await self._request_with_ratelimit(
                        client, "post", url, json=payload
                    )
                    if r.status_code >= 400:
                        _log.error(
                            "discord.enriched_compact_failed",
                            alertname=alertname, severity=severity,
                            status=r.status_code, body=r.text[:400],
                        )
                        return False
            except Exception as e:
                _log.error(
                    "discord.enriched_compact_exception",
                    alertname=alertname, severity=severity,
                    error=type(e).__name__, message=str(e)[:200],
                )
                return False
            return True

        # Stage 2: PATCH-dedup. Раньше send_enriched_alert POSTил на каждую
        # (alertname, severity)-группу AM batch'а — без content-dedup,
        # 18 embed/сутки в preprod (group_interval=10m, repeat=4h).
        # Теперь — content-key (alertname,ns,service,severity)+30-мин окно
        # → 1 POST + N PATCH (counter в footer'е).
        return await self._post_or_patch_enriched(
            url=url,
            payload=payload,
            embed=payload["embeds"][0],
            alertname=alertname,
            # Sorted join: namespaces идут в порядке AM-batch'а, который
            # не стабилен между ре-mission'ами — namespaces[0] флипал ключ
            # дедупа у multi-ns групп (новый POST вместо PATCH).
            namespace=("+".join(sorted(namespaces)) if namespaces else None),
            service=head.service,
            severity=severity,
        )

    async def _post_or_patch_enriched(
        self,
        url: str,
        payload: Dict[str, Any],
        embed: Dict[str, Any],
        alertname: str,
        namespace: Optional[str],
        service: Optional[str],
        severity: str,
    ) -> bool:
        """PATCH-dedup для enriched-канала. Аналог `_post_or_edit_incident`,
        но с собственным кэшем `_recent_enriched` и без burst-агрегации
        (#9 здесь не нужна — enriched уже схлопывает AM batch в один embed).

        Логика:
          1. Если ключ (alertname,ns,service,severity) уже в кэше <TTL —
             PATCH сообщения (count++, обновляем footer).
          2. Иначе POST с ?wait=true, сохраняем msg_id+embed+ts.
          3. Без msg_id (legacy webhook без wait) — не пополняем кэш,
             следующий incident пойдёт как новый POST. Это OK.

        TTL берётся из `settings.ENRICHED_DEDUP_WINDOW_SECONDS` (default 30 мин).
        """
        now = time.time()
        ttl = int(getattr(settings, "ENRICHED_DEDUP_WINDOW_SECONDS", 1800) or 1800)
        key = _compute_enriched_key(
            alertname=alertname,
            namespace=namespace,
            service_name=service,
            severity=severity,
        )
        if key is None:
            # Без alertname или невалидный вход — POST без dedup.
            return await self._post_enriched_raw(url, payload)

        # Cross-replica store (PG, fallback на in-memory при недоступном PG):
        # per-process dict ломался на 2 репликах api — каждый под промахивался
        # мимо чужого кэша и дублировал POST с mention.
        # Атомарный claim-before-post (см. _post_or_edit_incident): закрывает
        # TOCTOU-окно get_fresh → POST → save между репликами.
        existing = dedup_store.claim(
            key, ttl_sec=ttl, now=now,
            alertname=alertname, namespace=namespace,
            service=service, severity=severity,
        )

        if existing is not None:
            if not existing.get("msg_id"):
                # Placeholder другой реплики (mid-post) — дубль не шлём.
                logging.info(
                    "discord_enriched_dedup_claimed_elsewhere key=%s", key,
                )
                # Placeholder живой реплики = embed в канале будет: не недоставка.
                return True
            await self._patch_enriched_recurrence(
                url=url, embed=embed, key=key,
                ttl_sec=ttl, now=now,
            )
            return True

        # Новый POST (claim наш). wait=true чтобы получить msg_id.
        post_url = _ensure_wait_param(url)
        msg_id: Optional[str] = None
        try:
            async with httpx.AsyncClient() as client:
                r = await self._request_with_ratelimit(
                    client, "post", post_url, json=payload
                )
                if r.status_code >= 400:
                    # Через structlog, а не `logging.error(..., extra=...)`:
                    # stdlib-форматтер `extra` не печатает, и в kubectl logs
                    # оставалась одна строка `ERROR:root:...` без кода и тела
                    # ответа. Замер 05.09.2026: 96 недоставок за сутки, все по
                    # одному алерту, и причина не диагностировалась в принципе.
                    #
                    # Тело Discord отдаёт с точным именем поля
                    # (`embeds.0.fields.25`, `BASE_TYPE_MAX_LENGTH`) — режем
                    # его на 400 символов, а не на 200: 200 обрывались ровно
                    # перед этой частью.
                    _log.error(
                        "discord.enriched_post_failed",
                        alertname=alertname, severity=severity,
                        namespace=namespace, service=service,
                        status=r.status_code, body=r.text[:400],
                        fields=len(embed.get("fields") or []),
                        embed_len=_embed_total_len(embed),
                    )
                    # POST не случился — отпускаем claim.
                    dedup_store.release(key)
                    return False
                if r.status_code == 200:
                    try:
                        msg_id = str(r.json().get("id") or "") or None
                    except (ValueError, TypeError):
                        msg_id = None
        except Exception as e:
            _log.error(
                "discord.enriched_post_exception",
                alertname=alertname, severity=severity,
                namespace=namespace, service=service,
                error=type(e).__name__, message=str(e)[:200],
            )
            dedup_store.release(key)
            return False

        if not msg_id:
            # Legacy webhook без wait=true → нечего PATCH-ить, dedup-кэш
            # не пополняем (контракт прежний: следующий firing = новый POST).
            # Claim отпускаем — это симметрично с `_post_or_edit_incident`.
            dedup_store.release(key)
            # POST прошёл (2xx), просто нечего PATCH-ить — доставка состоялась.
            return True

        # Без webhook_url: enriched-канал всегда шлёт на
        # settings.DISCORD_WEBHOOK_URL, PATCH перечитает его из настроек.
        dedup_store.save(
            key,
            msg_id=msg_id,
            embed=embed,
            alertname=alertname,
            namespace=namespace,
            service=service,
            severity=severity,
            now=now,
        )
        return True

    async def send_resolved_notice(
        self,
        *,
        alertname: str,
        namespace: Optional[str] = None,
        service: Optional[str] = None,
        duration_min: Optional[int] = None,
    ) -> None:
        """Короткий зелёный embed «critical-алерт разрезолвился».

        Инварианты: без mention (хорошими новостями никого не пингуем),
        без dedup-кэша (резолв одноразовый), только из enrich-and-forward
        и только для critical — фильтр на стороне вызывающего.
        """
        url = settings.DISCORD_WEBHOOK_URL
        if not url:
            return
        target = "/".join(p for p in (namespace, service) if p)
        duration_label = ""
        if duration_min is not None and duration_min >= 0:
            h, m = divmod(duration_min, 60)
            duration_label = f", длился {h}ч {m}м" if h else f", длился {m}м"
        embed = {
            "title": f"✅ resolved: {alertname}"[:256],
            "description": f"{target}{duration_label}"[:4096],
            "color": SEVERITY_COLOR_RESOLVED,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        payload = {"embeds": [embed], "allowed_mentions": {"parse": []}}
        if settings.DISCORD_DRY_RUN:
            _dry_run_log.info(
                "discord.dry_run.send_resolved_notice",
                alertname=alertname, namespace=namespace, service=service,
                duration_min=duration_min,
            )
            return
        await self._post_enriched_raw(url, payload)

    async def _post_enriched_raw(self, url: str, payload: Dict[str, Any]) -> bool:
        """Fallback-POST когда _compute_enriched_key вернул None.

        Без dedup — просто шлём embed. Возвращает delivered (см. контракт
        send_enriched_alert): False на HTTP>=400, чтобы вызывающая сторона
        могла откатить tentative-инкремент chronic-счётчика.
        """
        async with httpx.AsyncClient() as client:
            r = await self._request_with_ratelimit(
                client, "post", url, json=payload
            )
            if r.status_code >= 400:
                logging.error(
                    "discord_enriched_alert_failed",
                    extra={"status": r.status_code, "body": r.text[:200]},
                )
                return False
        return True

    async def _patch_enriched_recurrence(
        self,
        url: str,
        embed: Dict[str, Any],
        key: str,
        ttl_sec: int,
        now: float,
    ) -> None:
        """PATCH ранее отправленного enriched embed: count++, footer update.

        В отличие от `_patch_recurrence` (incident-канал) — нет mode=linked
        (enriched уже агрегирует AM batch). Footer формата
        `<base> · ×N в <TTL>мин · first HH:MM · last HH:MM` для consistency
        с incident-каналом.
        """
        rec = dedup_store.bump(key, now=now)
        if rec is None:
            # Запись исчезла между get_fresh и bump (purge/race) —
            # recurrence фиксировать не на чем, молча выходим.
            return
        msg_id = rec["msg_id"]
        cached_embed = rec.get("embed") or embed
        count = rec["count"]
        first_ts = rec["first_ts"]
        # `url` = settings.DISCORD_WEBHOOK_URL, прочитанный на входе в
        # send_enriched_alert. В store токена больше нет.
        webhook_url = url

        # Берём новый embed (с актуальными KG-данными — KG mог обновиться
        # за окно дедупа), но обновляем footer counter'ом.
        patched_embed = dict(embed)
        first_seen = datetime.fromtimestamp(first_ts, tz=timezone.utc).strftime("%H:%M")
        last_seen = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%H:%M")
        original_footer = (patched_embed.get("footer") or {}).get("text") or ""
        # Если footer уже PATCH-ев (содержит «×N в»), берём префикс до « · ×».
        base_footer = original_footer.split(" · ×", 1)[0] if original_footer else ""
        ttl_min = max(1, ttl_sec // 60)
        patched_embed["footer"] = {
            "text": (
                f"{base_footer} · ×{count} в {ttl_min}мин · "
                f"first {first_seen} · last {last_seen}"
            )[:2048]
        }
        patch_payload = {"embeds": [patched_embed]}

        endpoint = _webhook_edit_endpoint(webhook_url, msg_id)
        if not endpoint:
            logging.warning(
                "discord_enriched_patch_no_endpoint",
                extra={"webhook": webhook_url[:40]},
            )
            return
        try:
            async with httpx.AsyncClient() as client:
                r = await self._request_with_ratelimit(
                    client, "patch", endpoint, json=patch_payload
                )
                if r.status_code >= 400:
                    logging.warning(
                        "discord_enriched_patch_failed",
                        extra={"status": r.status_code, "body": r.text[:200]},
                    )
                    return
        except Exception as e:
            logging.warning("discord_enriched_patch_exception", extra={"error": str(e)})
            return
        # Кэшируем patched_embed чтобы следующий patch не терял footer'ной
        # истории, если KG-данные между ре-mission'ами одинаковы.
        dedup_store.update_embed(key, patched_embed)
        # Audit-log в structlog (симметрично с incident-каналом).
        try:
            _log.info(
                "enriched.dedup_hit",
                key=key[:12], count=count, alertname=cached_embed.get("title", "?")[:40],
            )
        except Exception:
            pass

    async def send_external_probe_alert(
        self,
        host: str,
        status: str,
        snapshot: Dict[str, Any],
        resolved: bool = False,
    ) -> None:
        """Compact embed для external probe state-change.

        firing: 🔴 down или 🟡 degraded — color по severity, поле "IPs" с
        per-IP TCP/HTTP results, поле "HTTPS" с общим HEAD-кодом.
        resolved: ✅ — green, краткая строка.
        """
        if settings.DISCORD_DRY_RUN:
            _dry_run_log.info(
                "discord.dry_run.send_external_probe_alert",
                host=host, status=status, resolved=resolved,
            )
            return
        url = settings.DISCORD_WEBHOOK_URL
        if not url:
            logging.warning("DISCORD_WEBHOOK_URL not set, skipping external probe alert")
            return

        if resolved:
            title = f"✅ External probe recovered: {host}"
            color = _COLOR_RESOLVED
        elif status == "down":
            title = f"🔴 External probe DOWN: {host}"
            color = _COLOR_CRITICAL
        else:
            title = f"🟡 External probe degraded: {host}"
            color = _COLOR_WARNING

        ip_lines: List[str] = []
        for r in (snapshot.get("tcp_results") or []):
            ok = r.get("tcp_ok")
            mark = "✓" if ok else "✗"
            err = (r.get("error") or "")[:60]
            ms = r.get("latency_ms")
            ms_s = f"{ms}ms" if ms is not None else "—"
            ip_lines.append(f"`{mark}` `{r.get('ip','?'):<15}` tcp={ms_s} {err}")
        if not ip_lines and snapshot.get("dns_error"):
            ip_lines.append(f"DNS: `{snapshot['dns_error']}`")

        http = snapshot.get("http_result") or {}
        http_line = f"code=`{http.get('http_code', '—')}` latency=`{http.get('latency_ms', '—')}ms`"
        if http.get("error"):
            http_line += f" err=`{http['error'][:80]}`"

        fields = [
            {"name": "IPs", "value": "\n".join(ip_lines)[:1024] or "—", "inline": False},
            {"name": "HTTPS HEAD", "value": http_line[:1024], "inline": False},
        ]
        cf = snapshot.get("consecutive_failures")
        if cf and not resolved:
            fields.append({"name": "Consecutive failures", "value": f"`{cf}`", "inline": True})

        payload = {
            "embeds": [{
                "title": title[:256],
                "color": color,
                "fields": fields,
                "footer": {"text": f"external_probe/{host}"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }]
        }
        async with httpx.AsyncClient() as client:
            # #3: 429 в alert-storm (probe-алерты идут вместе с ним) ретраится.
            r = await self._request_with_ratelimit(client, "post", url, json=payload)
            if r.status_code >= 400:
                logging.error(
                    "discord_external_probe_alert_failed",
                    extra={"status": r.status_code, "body": r.text[:200], "host": host},
                )

    async def send_self_health_alert(
        self,
        failed_checks: List[Dict[str, Any]],
        warn_checks: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Single embed на отдельный dev-канал команды copilot.

        НЕ шлёт в DISCORD_WEBHOOK_URL (#infra-error) — намеренно, чтобы не
        смешивать «KG сам поломался» с «production-сервис упал». Адресат —
        команда разработчиков копилота, читающие свой канал.
        """
        url = settings.DISCORD_WEBHOOK_SELF_HEALTH_URL
        if not url:
            _log.info(
                "discord.self_health.skipped_no_url",
                failed=len(failed_checks),
            )
            return
        if settings.DISCORD_DRY_RUN:
            _dry_run_log.info(
                "discord.dry_run.send_self_health_alert",
                failed=len(failed_checks),
                warn=len(warn_checks or []),
            )
            return

        fields: List[Dict[str, Any]] = []
        for c in failed_checks[:10]:
            detail = c.get("detail") or {}
            summary = _summarize_self_health_detail(c.get("name") or "", detail)
            fields.append({
                "name": f"FAIL: {c.get('name', '?')}",
                "value": summary[:1024] or "—",
                "inline": False,
            })
        if warn_checks:
            warn_names = ", ".join(c.get("name", "?") for c in warn_checks[:10])
            fields.append({
                "name": f"warn ({len(warn_checks)})",
                "value": warn_names[:1024],
                "inline": False,
            })

        payload = {
            "embeds": [{
                "title": f"KG self-health: {len(failed_checks)} failed check(s)"[:256],
                "color": _COLOR_CRITICAL,
                "fields": fields,
                "footer": {"text": "kg_self_health_check"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }]
        }
        async with httpx.AsyncClient() as client:
            # #3: self-health алерт нужен ровно тогда, когда всё горит и Discord
            # рейтлимитит — 429 ретраится, а не глотается.
            r = await self._request_with_ratelimit(client, "post", url, json=payload)
            if r.status_code >= 400:
                logging.error(
                    "discord_self_health_alert_failed",
                    extra={"status": r.status_code, "body": r.text[:200]},
                )

    async def send_stuck_alerts_escalation(
        self,
        team_groups: List[Dict[str, Any]],
        total_count: int,
        min_duration_hours: int,
    ) -> None:
        """Single embed на dedicated escalation-webhook.

        НЕ шлёт в DISCORD_WEBHOOK_URL (#infra-error) — у stuck-alerts другая
        аудитория: owner-команды, а не on-call. Если URL не задан — skip
        (audit-log при этом остаётся, см. tasks.kg_stuck_alerts_check_task).
        """
        url = settings.DISCORD_WEBHOOK_STUCK_ALERTS_URL
        if not url:
            _log.info(
                "discord.stuck_alerts.skipped_no_url",
                total=total_count,
                teams=len(team_groups),
            )
            return
        if settings.DISCORD_DRY_RUN:
            _dry_run_log.info(
                "discord.dry_run.send_stuck_alerts_escalation",
                total=total_count,
                teams=len(team_groups),
            )
            return

        fields: List[Dict[str, Any]] = []
        # Не больше 25 полей на embed — лимит Discord. Берём top-15 команд.
        for tg in team_groups[:15]:
            team = tg.get("team_owner") or "unknown"
            alerts = tg.get("alerts") or []
            lines: List[str] = []
            for a in alerts[:10]:
                hours = a.get("hours_firing", 0.0)
                svc = a.get("service") or "—"
                name = a.get("alertname") or "?"
                rec = a.get("recurrence_24h") or 0
                lines.append(
                    f"• `{name}` — `{svc}` · firing {hours:.0f}h"
                    + (f" · 24h fires: {rec}" if rec > 1 else "")
                )
            if not lines:
                continue
            field_val = "\n".join(lines)
            fields.append({
                "name": f"{team} ({len(alerts)})",
                "value": field_val[:1024],
                "inline": False,
            })

        embed: Dict[str, Any] = {
            "title": (
                f"🔴 {total_count} stuck alerts "
                f"(>{min_duration_hours}h firing)"
            )[:256],
            "color": _COLOR_CRITICAL,
            "fields": fields,
            "footer": {"text": "runbook: ..."},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        # #6: 15 полей × до 1024 chars — потенциально >6000 TOTAL → Discord 400
        # → эскалация дропалась бы целиком именно на больших завалах.
        _fit_embed_to_limit(embed)
        payload = {"embeds": [embed]}
        async with httpx.AsyncClient() as client:
            # #3: 429 в alert-storm ретраится, а не глотается.
            r = await self._request_with_ratelimit(client, "post", url, json=payload)
            if r.status_code >= 400:
                logging.error(
                    "discord_stuck_alerts_escalation_failed",
                    extra={"status": r.status_code, "body": r.text[:200]},
                )

    # send_approval_request удалён (legacy approve-URL flow).
    # Новый button-based flow: api/discord_interactions.py (PR #12).


discord_service = DiscordService()
