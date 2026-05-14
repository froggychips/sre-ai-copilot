import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import httpx

from app.config import settings

if TYPE_CHECKING:
    from app.core.execution_dsl import ExecutionIntent

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
            logging.info("[DISCORD_DRY_RUN] send_report:\n%s", report_text)
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
            logging.info("[DISCORD_DRY_RUN] send_stats_report:\n%s", content)
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
            logging.info("[DISCORD_DRY_RUN] send_incident_report: %s | cause=%s | rq=%s",
                         title, cause, resolution_quality)
            return
        url = settings.DISCORD_WEBHOOK_URL
        if not url:
            logging.warning("DISCORD_WEBHOOK_URL not set, skipping incident report")
            return
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=payload)
            if r.status_code >= 400:
                logging.error("discord_incident_report_failed", extra={"status": r.status_code})

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
