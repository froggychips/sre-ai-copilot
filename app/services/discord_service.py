import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import httpx
import structlog

from app.config import settings

# structlog для DRY_RUN-логов — стандартный python `logging` отфильтровывается
# на корневом WARNING level в production, поэтому [DISCORD_DRY_RUN] раньше
# не появлялись в kubectl logs. structlog идёт через тот же sink что и
# kg.populate.done / enrich_forward.suppress_chronic — visibility гарантирована.
_dry_run_log = structlog.get_logger("discord.dry_run")

if TYPE_CHECKING:
    from app.core.execution_dsl import ExecutionIntent
    from app.services.alert_enrichment import EnrichedContext

# Discord embed colour codes
_COLOR_CRITICAL = 0xE53935   # red
_COLOR_WARNING  = 0xFDD835   # yellow
_COLOR_RESOLVED = 0x43A047   # green
_COLOR_UNKNOWN  = 0x9E9E9E   # grey

_SEVERITY_COLORS = {
    "critical": _COLOR_CRITICAL,
    "warning":  _COLOR_WARNING,
    "info":     _COLOR_UNKNOWN,
}


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
            await client.post(url, json=payload)

    async def send_stats_report(self, content: str) -> None:
        """Отправить markdown-content в канал #stats как Discord embed.

        Используем embed (description-limit 4096) вместо content (limit 2000).
        Daily-digest сейчас ~2800 chars — в content не влезет.

        Первая строка контента вынесена в embed.title (если bold + emoji),
        остальное — в description.
        """
        url = settings.DISCORD_WEBHOOK_STATS_URL
        if not url:
            logging.warning("DISCORD_WEBHOOK_STATS_URL not set, skipping stats report")
            return
        if settings.DISCORD_DRY_RUN:
            _dry_run_log.info("discord.dry_run.send_stats_report", content=content[:500])
            return

        lines = content.split("\n", 1)
        if len(lines) == 2 and lines[0].strip():
            title = lines[0].strip()[:256]  # Discord title-limit
            description = lines[1].lstrip("\n")
        else:
            title = "Stats digest"
            description = content
        # Embed description hard-limit 4096; truncate с маркером.
        if len(description) > 4000:
            description = description[:3990] + "\n_…truncated_"

        payload = {
            "embeds": [{
                "title": title,
                "description": description,
                "color": 0x607D8B,  # blue-grey, нейтральный для аналитики
            }]
        }
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=payload)
            if r.status_code >= 400:
                logging.error(
                    "discord_stats_report_failed",
                    extra={"status": r.status_code, "body": r.text[:200]},
                )

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
    ) -> None:
        """Единый embed-отчёт, заменяющий сырой алерт от Spidey Bot.

        Формат: заголовок алерта (что видел Spidey Bot) + root cause +
        краткий вывод пайплайна — всё в одном Discord-сообщении.
        """
        color = (
            _COLOR_RESOLVED if resolution_quality == "resolved"
            else _SEVERITY_COLORS.get(severity.lower(), _COLOR_UNKNOWN)
        )

        status_icon = "✅" if resolution_quality == "resolved" else "⚠️"
        recurrence_tag = " · 🔁 RECURRENCE" if is_recurrence else ""
        flap_tag = f" · 🔄 ×{flap_count}" if flap_count > 0 else ""
        ns_part = f" · {namespace}" if namespace else ""
        title = f"{status_icon} {alertname}{ns_part}{recurrence_tag}{flap_tag}"

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
        action_row = [
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
                "custom_id": f"apply_{incident_id}",
            })

        payload = {
            "embeds": [{
                "title": title,
                "color": color,
                "fields": fields,
                "description": description,
                "footer": {"text": f"incident/{incident_id}"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }],
            "components": [{"type": 1, "components": action_row}],
        }

        if settings.DISCORD_DRY_RUN:
            _dry_run_log.info(
                "discord.dry_run.send_incident_report",
                title=title, cause=cause, resolution_quality=resolution_quality,
            )
            return
        url = settings.DISCORD_WEBHOOK_URL
        if not url:
            logging.warning("DISCORD_WEBHOOK_URL not set, skipping incident report")
            return
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=payload)
            if r.status_code >= 400:
                logging.error("discord_incident_report_failed", extra={"status": r.status_code})

    async def send_enriched_alert(
        self,
        contexts: List["EnrichedContext"],
        env: Optional[str] = None,
        resurfaced: bool = False,
    ) -> None:
        """Детерминированный embed с KG-контекстом, БЕЗ LLM.

        Принимает batch `EnrichedContext` (несколько алертов одного типа в
        одном AM-batch'е сворачиваются в один embed). Старый Spidey Bot
        слал по сообщению на каждый алерт; здесь один embed на logical-group.

        Не вызывает модель. Latency бюджет — <500ms p95 синхронно
        в HTTP-handler'е /webhooks/alertmanager/enrich-and-forward.
        """
        if not contexts:
            return
        head = contexts[0]
        incident = head.incident
        labels = incident.labels
        alertname = labels.get("alertname", "unknown")
        severity = (incident.severity or "unknown").lower()

        # Цвет + emoji
        color = _SEVERITY_COLORS.get(severity, _COLOR_UNKNOWN)
        if head.rollout_noise:
            color = _COLOR_UNKNOWN
        icon = {"critical": "🔴", "warning": "🟡"}.get(severity, "⚪")
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
        svc_or_pod = head.service or head.pod or "?"

        recurrence_tag = ""
        rec_max = max((len(c.recurrence_24h) for c in contexts), default=0)
        if rec_max >= 2:
            recurrence_tag = f" · 🔁 ×{rec_max} за 24h"
        noise_tag = " · 🤖 ROLLOUT-NORMAL" if head.rollout_noise else ""
        ns_tag = f" ({len(namespaces)} ns)" if len(namespaces) > 1 else ""
        resurfaced_tag = " · 🌀 RESURFACED" if resurfaced else ""

        title = (
            f"{icon} {env_part}{alertname} · {svc_or_pod}{ns_tag}"
            f"{recurrence_tag}{noise_tag}{resurfaced_tag}"
        )

        fields: List[Dict[str, Any]] = []
        fields.append({
            "name": "Namespaces",
            "value": f"`{ns_str}`",
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

        # Recent deploys. Окно вычисляется из самого дальнего deploy —
        # alert_enrichment может использовать fallback 7д если узкое окно
        # пусто; заголовок честно показывает фактический диапазон.
        if head.recent_deploys:
            lines = []
            max_min = 0
            for d in head.recent_deploys[:3]:
                mins = d.get("minutes_before_incident", 0)
                try:
                    max_min = max(max_min, int(mins))
                except (ValueError, TypeError):
                    pass
                sha = (d.get("sha") or "")[:7]
                num = d.get("number") or "?"
                bt = d.get("buildtype_id") or ""
                status = d.get("status") or ""
                lines.append(
                    f"• `{num}` {sha} — {mins} мин назад"
                    + (f" ({bt})" if bt else "")
                    + (f" — {status}" if status else "")
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

        # Upstream алертит сейчас
        if head.upstream_alerts:
            lines = []
            for a in head.upstream_alerts[:5]:
                svc = a.get("service") or "?"
                ns = a.get("namespace") or "?"
                an = a.get("alertname") or "?"
                mins = a.get("minutes_before", "?")
                ek = a.get("edge_kind") or ""
                lines.append(f"• ✗ `{svc}` @ `{ns}` — `{an}` ({mins}m назад, edge={ek})")
            fields.append({
                "name": "Upstream сейчас (KG)",
                "value": "\n".join(lines)[:1024],
                "inline": False,
            })

        # Outgoing deps — куда сервис сам ходит. Для leaf-сервисов (как
        # bot-service) это главная диагностика при падении: «упал —
        # потому что зависит от X». Группируем по kind.
        if head.outgoing_deps:
            by_kind: Dict[str, List[str]] = {}
            for d in head.outgoing_deps:
                k = d.get("kind", "?")
                target = f"`{d.get('service','?')}`"
                target_ns = d.get("namespace") or ""
                if target_ns and target_ns != (head.incident.namespace or ""):
                    target = f"{target} @ `{target_ns}`"
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
                "name": "🔗 Зависит от (outgoing, KG)",
                "value": "\n".join(lines)[:1024],
                "inline": False,
            })

        # Inbound callers — сколько сервисов вызывают этот.
        # Для high-fan-in узлов (общая БД, NATS cluster) это сигнал blast radius.
        if head.inbound_count_by_kind:
            parts = [f"{cnt} через `{k}`" for k, cnt in head.inbound_count_by_kind.items()]
            fields.append({
                "name": "Inbound callers (KG)",
                "value": ", ".join(parts),
                "inline": False,
            })

        # Recent pod_events (kg_pod_events) — k8s diagnostic signal
        # (OOMKilled / ImagePullBackOff / BackOff / Unhealthy / ...).
        if head.pod_events:
            lines = []
            for ev in head.pod_events[:5]:
                reason = ev.get("reason", "?")
                count = ev.get("count")
                mins = ev.get("minutes_before", "?")
                msg = (ev.get("message") or "").replace("\n", " ")[:80]
                cnt_part = f" ×{count}" if count and count > 1 else ""
                lines.append(f"• 🩺 `{reason}`{cnt_part} — {mins} мин назад: {msg}")
            fields.append({
                "name": "Recent pod events (k8s)",
                "value": "\n".join(lines)[:1024],
                "inline": False,
            })

        # Hypothesis — rule-based, без LLM
        hyp = head.primary_hypothesis()
        if hyp:
            fields.append({
                "name": "Гипотеза (rule-based, без LLM)",
                "value": hyp[:1024],
                "inline": False,
            })

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
                f"_KG topology snapshot {head.kg_data_age_sec // 60} мин назад — может быть stale._"
            )
        description = "\n".join(description_lines)[:1200]

        payload = {
            "embeds": [{
                "title": title[:256],
                "color": color,
                "fields": fields,
                "description": description,
                "footer": {"text": f"copilot/enrich · groupKey={(labels.get('alertname') or '?')}"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }],
            # Mention-payload пуст: даже если в будущем добавим <@&role> в title,
            # пользователей не пинговать пока owner-mapping не проверен на проде.
            "allowed_mentions": {"parse": []},
        }

        if settings.DISCORD_DRY_RUN:
            # Главный путь — это то место где видно фактический embed-output
            # KG-enrichment. Структурированные поля позволяют отфильтровать
            # ровно тот логи-stream в kubectl logs / VictoriaLogs.
            _dry_run_log.info(
                "discord.dry_run.send_enriched_alert",
                title=title,
                namespaces=ns_str,
                hypotheses=hyp,
                contexts_count=len(contexts),
                severity=severity,
                resurfaced=resurfaced,
                rollout_noise=head.rollout_noise,
            )
            return
        url = settings.DISCORD_WEBHOOK_URL
        if not url:
            logging.warning("DISCORD_WEBHOOK_URL not set, skipping enriched alert")
            return
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=payload)
            if r.status_code >= 400:
                logging.error(
                    "discord_enriched_alert_failed",
                    extra={"status": r.status_code, "body": r.text[:200]},
                )

    async def send_approval_request(self, approval_id: str, details: dict):
        """Отправляет Rich Embed с информацией об опасной операции и ссылками для подтверждения."""
        payload = {
            "embeds": [
                {
                    "title": "⚠️ ACTION APPROVAL REQUIRED",
                    "description": "AI Agent requested a high-risk operation on Kubernetes.",
                    "color": 16711680,
                    "fields": [
                        {
                            "name": "Command",
                            "value": f"`{details.get('command')}`",
                            "inline": False,
                        },
                        {
                            "name": "Risk Level",
                            "value": details.get("risk", "HIGH"),
                            "inline": True,
                        },
                        {"name": "Approval ID", "value": approval_id, "inline": True},
                        {
                            "name": "Action",
                            "value": f"[APPROVE](https://your-api.com/approvals/{approval_id}/approve) | [REJECT](https://your-api.com/approvals/{approval_id}/reject)",
                            "inline": False,
                        },
                    ],
                }
            ]
        }
        url = settings.DISCORD_WEBHOOK_URL
        if not url:
            logging.warning("DISCORD_WEBHOOK_URL not set, skipping approval request")
            return
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload)
            if response.status_code >= 400:
                logging.error(
                    "discord_approval_request_failed",
                    extra={"status_code": response.status_code},
                )


discord_service = DiscordService()
