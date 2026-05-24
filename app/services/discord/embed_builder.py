"""Embed field builders для Discord-сообщений.

Чистые функции — берут данные, возвращают field dict / string. Без I/O
(кроме `_build_log_error_rate_field`, который читает kg_log_observations
через SQLAlchemy — выделен из общей логики потому что best-effort и
изолирован try/except).
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, cast

import structlog

from app.services.pii_redaction import redact_pii

_log = structlog.get_logger("discord.embed_builder")


def _summarize_self_health_detail(name: str, detail: Dict[str, Any]) -> str:
    """Сжать detail check'а в одну Discord-строку.

    Не json.dumps — детали бывают вложенные (per_metric/per_task), читать в
    embed-е невозможно. Здесь делаем «одно предложение» на каждый тип чека.
    """
    if name == "materialization_zero_rate":
        offenders = []
        for metric, info in (detail.get("per_metric") or {}).items():
            if info.get("status") in {"warn", "fail"}:
                offenders.append(f"{metric}={info.get('zero_or_null_pct')}%")
        return f"zero/null rate too high: {', '.join(offenders) or '—'}"
    if name == "sync_lag":
        offenders = []
        for task, info in (detail.get("per_task") or {}).items():
            if info.get("status") in {"warn", "fail"}:
                lag = info.get("lag_minutes")
                offenders.append(f"{task}: lag={lag}min")
        return f"stale: {', '.join(offenders) or '—'}"
    if name == "pod_events_link_rate":
        return f"linked={detail.get('linked_pct')}% of {detail.get('total')} events"
    if name == "edges_freshness":
        return f"stale={detail.get('stale_pct')}% of {detail.get('total')} edges"
    if name == "anomaly_signal_health":
        return f"count_24h={detail.get('count_24h')} — {detail.get('reason')}"
    if name == "alerts_resolve_freshness":
        return f"stale_open={detail.get('stale_open_alerts')} alerts >7d unresolved"
    # generic fallback
    if "error" in detail:
        return f"error: {detail['error']}"
    return ", ".join(f"{k}={v}" for k, v in list(detail.items())[:4])


def _format_sha_link(sha: Optional[str], repo: Optional[str] = None) -> str:
    """Markdown-ссылка на коммит в gitlab или короткий plain sha.

    repo может быть полным URL-проектом (https://wo-gitlab.../<group>/<proj>)
    или path `<group>/<proj>`. Если sha нет — пустая строка. Если repo нет —
    plain короткий sha. Это helper для #2 и #7.
    """
    if not sha:
        return ""
    short = sha[:8]
    if not repo:
        return f"`{short}`"
    # Поддерживаем оба формата: уже-URL или просто path.
    if repo.startswith("http://") or repo.startswith("https://"):
        base = repo.rstrip("/")
    else:
        base = f"https://wo-gitlab.lastoasisgame.com/{repo.strip('/')}"
    return f"[`{short}`]({base}/-/commit/{sha})"


def _build_deploy_correlation_field(corr: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Сформировать embed-field для suspect-deploy (#2).

    Возвращает None если verdict в {None, "ok", "unlikely"} или deploy
    отсутствует. Показывается для verdict in {"likely", "suspect", "weak"}
    с указанием confidence-скора.
    """
    if not corr:
        return None
    verdict = corr.get("verdict")
    # "ok" — legacy alias; новые verdict-ы: likely/suspect/weak/unlikely.
    if verdict not in ("likely", "suspect", "weak"):
        return None
    deploy = corr.get("deploy") or {}
    if not deploy:
        return None

    bt_id = deploy.get("buildtype_id") or "?"
    build_num = deploy.get("build_number") or "?"
    started_at = deploy.get("started_at") or "?"
    mins = deploy.get("minutes_before_incident")
    triggered = deploy.get("triggered_by") or ""
    sha = deploy.get("sha")
    repo = deploy.get("repo")
    confidence = corr.get("confidence")

    head_line = f"`{bt_id}` #{build_num} @ {started_at}"
    if mins is not None:
        head_line += f" ({mins}min before)"
    if confidence is not None:
        head_line += f" (score {confidence:.2f} · {verdict})"

    sha_link = _format_sha_link(sha, repo)
    sha_line = ""
    if sha_link:
        sha_line = f"sha: {sha_link}"
    if triggered:
        sha_line = (sha_line + " by " if sha_line else "by ") + f"`{triggered}`"

    metric_parts: List[str] = []
    diffs = corr.get("metrics_diff") or {}
    metric_aliases = {
        "p95_latency_ms": "p95",
        "http_5xx_rate": "5xx",
        "cpu_pct": "cpu",
        "mem_pct": "mem",
        "restarts_rate": "restarts",
    }
    for metric, label in metric_aliases.items():
        d = (diffs.get(metric) or {}).get("delta_pct")
        if d is None:
            continue
        # Только реальные spike-и (>50% = тот же порог, что и verdict).
        if d > 50.0:
            sign = "+" if d > 0 else ""
            metric_parts.append(f"{label} {sign}{int(d)}%")
    metric_line = ", ".join(metric_parts) if metric_parts else ""

    value_lines = [head_line]
    if sha_line:
        value_lines.append(sha_line)
    if metric_line:
        value_lines.append(metric_line)

    # Иконка зависит от тира — выше тир, краснее. Чтобы оператор не путал
    # «likely» с «weak» по первой строке embed-а.
    icon = {"likely": "🔴", "suspect": "🟠", "weak": "🟡"}.get(verdict, "🟡")
    field_name = f"{icon} Suspect Deploy" if verdict != "weak" else f"{icon} Weak Deploy Signal"
    return {
        "name": field_name,
        "value": "\n".join(value_lines)[:1024],
        "inline": False,
    }


def _build_log_error_rate_field(
    service: Optional[str],
    namespace: Optional[str],
    incident_ts: Optional[datetime],
) -> Optional[Dict[str, Any]]:
    """Field 📜 Log error rate ±10min (#8).

    Резолвит service_id по (service, namespace), запрашивает kg_log_observations
    в окне [incident_ts - 10min, incident_ts + 10min] и собирает Error/Fatal
    count + sample message. Возвращает None если нет матча / нет данных.
    """
    if not service or not namespace or incident_ts is None:
        return None
    try:
        # Локальные импорты — чтобы не утягивать SQLAlchemy в чистые dry-run
        # пути (тесты, которые мокают DB).
        from app.database import SessionLocal
        from app.knowledge_graph.schema import LogObservation, Service

        # incident_ts может быть aware — kg_log_observations.ts naive UTC.
        ts_naive = incident_ts
        if ts_naive.tzinfo is not None:
            ts_naive = ts_naive.astimezone(timezone.utc).replace(tzinfo=None)
        window_start = ts_naive - timedelta(minutes=10)
        window_end = ts_naive + timedelta(minutes=10)

        db = SessionLocal()
        try:
            svc = (
                db.query(Service)
                .filter(Service.namespace == namespace, Service.name == service)
                .one_or_none()
            )
            if svc is None:
                return None
            rows = (
                db.query(LogObservation)
                .filter(
                    LogObservation.service_id == svc.id,
                    LogObservation.ts >= window_start,
                    LogObservation.ts <= window_end,
                    LogObservation.level.in_(["Error", "Fatal"]),
                )
                .all()
            )
            if not rows:
                return None
            counts: Dict[str, int] = {}
            sample = ""
            sample_count = -1
            for r in rows:
                level = cast(str, r.level)
                row_count = int(r.count or 0)
                counts[level] = counts.get(level, 0) + row_count
                if row_count > sample_count and r.sample_message:
                    sample_count = row_count
                    sample = cast(str, r.sample_message)
            total = sum(counts.values())
            if total <= 0:
                return None
            parts = []
            for level in ("Error", "Fatal"):
                if counts.get(level):
                    parts.append(f"{level}: {counts[level]}")
            value = ", ".join(parts) if parts else f"total: {total}"
            if sample:
                # Defense-in-depth: seq_logs_sync redacts on write, but if a
                # future source pushes into kg_log_observations without
                # scrubbing, we still don't want PII / secrets surfacing
                # in Discord embeds. redact_pii is idempotent over already-
                # redacted placeholders, so this is a no-op on the common path.
                value += f"\n_sample:_ {redact_pii(sample)[:200]}"
            return {
                "name": "📜 Log error rate (±10min)",
                "value": value[:1024],
                "inline": False,
            }
        finally:
            db.close()
    except Exception as e:
        # Best-effort: embed уходит без поля, инцидент не валим.
        _log.warning("log_error_rate_field_failed", error=type(e).__name__)
        return None


def _build_blast_radius_field(
    blast: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Wave 7 (X, PR #71) — 🎯 Blast radius field для critical embed.

    Принимает результат `queries.blast_radius_for(...)`. Считает:
        * сколько Service'ов маршрутят трафик на упавший Deployment
          (`serves_traffic` IN-edges) — это «кто вызовет проблему»;
        * сколько Ingress hosts (внешние URL) затронуты (`routes_to`).

    Возвращает None если оба counts == 0 (skip-if-empty). Embed-секция
    добавляется только когда есть нечего показать кроме нулей.
    """
    if not blast:
        return None
    services = blast.get("services") or []
    urls = blast.get("urls") or []
    services_total = blast.get("services_total") or 0
    urls_total = blast.get("urls_total") or 0
    if services_total == 0 and urls_total == 0:
        return None

    parts: List[str] = []
    if services_total > 0:
        names = ", ".join(f"`{s}`" for s in services[:3])
        suffix = f" (+{services_total - len(services)})" if services_total > len(services) else ""
        parts.append(f"{services_total} svc → {names}{suffix}")
    if urls_total > 0:
        url_names = ", ".join(f"`{u}`" for u in urls[:3])
        suffix = f" (+{urls_total - len(urls)})" if urls_total > len(urls) else ""
        parts.append(f"{urls_total} URL → {url_names}{suffix}")

    return {
        "name": "🎯 Blast radius (Wave 7)",
        "value": "\n".join(parts)[:1024],
        "inline": False,
    }


def _build_nats_impact_field(
    impact: Optional[List[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    """Wave 7 (Z, PR #72) — 📨 NATS impact field для critical embed.

    Принимает результат `queries.nats_impact_for(...)`. Для каждого
    subject показывает direction (pub→/sub←), subject-имя и количество
    других сервисов на этом subject'е.

    Возвращает None если list пуст (большинство сервисов без NATS).
    Caching impact_count происходит внутри `nats_impact_for` (один batch
    SQL-запрос на все subjects), здесь — чисто рендеринг.
    """
    if not impact:
        return None
    lines: List[str] = []
    for entry in impact[:3]:
        subject = entry.get("subject") or "?"
        direction = (entry.get("direction") or "?").lower()
        impact_count = entry.get("impact_count") or 0
        # Чистим префикс `nats-subject:` если synthetic-узел так назван;
        # populator может использовать любой prefix, но render оставляет
        # имя как есть, если префикса нет.
        subject_clean = subject.split(":", 1)[1] if subject.startswith("nats-subject:") else subject
        arrow = "pub→" if direction == "pub" else ("sub←" if direction == "sub" else "?·")
        # Семантика impact_count: pub → сколько подписчиков получат событие;
        # sub → сколько других продюсеров (общий subject = «обмен»).
        if direction == "pub":
            count_label = f"{impact_count} sub-консьюмер{'ов' if impact_count != 1 else ''}"
        elif direction == "sub":
            count_label = f"{impact_count} co-consumer{'s' if impact_count != 1 else ''}"
        else:
            count_label = f"{impact_count} other"
        lines.append(f"• {arrow}`{subject_clean}` ({count_label})")

    return {
        "name": "📨 NATS impact (Wave 7)",
        "value": "\n".join(lines)[:1024],
        "inline": False,
    }


def _build_pod_trail_field(
    trail: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Wave 7 (Y, PR #70) — 🕒 Pod trail field для critical embed.

    Принимает результат `queries.pod_event_summary_for(...)`. Агрегирует
    PodEvent по reason за последний час: `5 evts: 3 OOMKilled,
    2 CrashLoopBackOff`. Это короткая сводка над уже-существующей
    секцией «Recent pod events» (top-5 individual events), но фокус
    на counts а не на messages.

    Возвращает None если total == 0 (skip-if-empty).
    """
    if not trail:
        return None
    total = trail.get("total") or 0
    by_reason = trail.get("by_reason") or []
    if total == 0 or not by_reason:
        return None
    # Top-5 reasons максимум — больше в embed-line не вмещается читаемо.
    by_reason = by_reason[:5]
    reasons_str = ", ".join(f"{cnt} {reason}" for reason, cnt in by_reason)
    return {
        "name": "🕒 Pod trail (Wave 7, 1h)",
        "value": f"{total} evts: {reasons_str}"[:1024],
        "inline": False,
    }


def _format_recurrence_tag(
    is_recurrence: bool,
    count_24h: int = 0,
    count_7d: int = 0,
) -> str:
    """#13 — recurrence label с окном.

    24h > 1 → `🔁 ×N in 24h`; 7d > 24h → добавить `· M in 7d`.
    Fallback на старый `🔁 RECURRENCE` если счётчики нулевые но is_recurrence=True
    (например тесты не пробрасывают counts).
    """
    if count_24h > 1:
        tag = f" · 🔁 ×{count_24h} in 24h"
        if count_7d > count_24h:
            tag += f" · {count_7d} in 7d"
        return tag
    if is_recurrence:
        return " · 🔁 RECURRENCE"
    return ""
