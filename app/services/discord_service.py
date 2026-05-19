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

        # Phase 3-A: severity-aware enrichment depth.
        # Warning embed'ов в 5-10× больше чем critical → noise сосредоточен
        # там. Для warning показываем «minimum viable» — title + owner +
        # most-likely-cause + why-this-matters. Для critical — полный embed.
        is_critical = severity == "critical"

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

        # Recent deploys. Окно вычисляется из самого дальнего deploy —
        # alert_enrichment может использовать fallback 7д если узкое окно
        # пусто; заголовок честно показывает фактический диапазон.
        # Phase 3-A: для warning — top-1 строка, для critical — full 3.
        if head.recent_deploys:
            lines = []
            max_min = 0
            top_n = 3 if is_critical else 1
            for d in head.recent_deploys[:top_n]:
                mins = d.get("minutes_before_incident", 0)
                try:
                    max_min = max(max_min, int(mins))
                except (ValueError, TypeError):
                    pass
                sha = (d.get("sha") or "")[:7]
                num = d.get("number") or "?"
                bt_name = d.get("buildtype_name") or d.get("buildtype_id") or "?"
                status = d.get("status") or ""
                triggered = d.get("triggered_by") or ""
                url = d.get("url")
                by_part = f" by `{triggered}`" if triggered else ""
                # Build label — кликабельный если есть TC URL.
                if url:
                    build_label = f"[{bt_name} #{num}]({url})"
                else:
                    build_label = f"`#{num}` ({bt_name})"
                sha_part = f" {sha}" if sha else ""
                status_part = f" — {status}" if status else ""
                lines.append(
                    f"• {build_label}{by_part}{sha_part} — {mins} мин назад{status_part}"
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
                if is_critical:
                    msg = (ev.get("message") or "").replace("\n", " ")[:80]
                    lines.append(f"• 🩺 `{reason}`{cnt_part} — {mins} мин назад: {msg}")
                else:
                    lines.append(f"• 🩺 `{reason}`{cnt_part} — {mins} мин назад")
            fields.append({
                "name": "Recent pod events (k8s)",
                "value": "\n".join(lines)[:1024],
                "inline": False,
            })

        # Phase 3-A: "Гипотеза" (legacy field) удалена — теперь
        # «🎯 Скорее всего» выше по полю primary_hypothesis(). Дубликат не нужен.

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
                hypothesis=head.primary_hypothesis(),
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
            r = await client.post(url, json=payload)
            if r.status_code >= 400:
                logging.error(
                    "discord_external_probe_alert_failed",
                    extra={"status": r.status_code, "body": r.text[:200], "host": host},
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
