import hashlib
import hmac
import re
from typing import Iterable, List, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import User, get_current_user
from app.config import settings
from app.core.state_machine import IncidentState, StateMachine
from app.security.replay import alertmanager_signature_cache, is_timestamp_fresh
from app.database import IncidentRecord, get_db
from app.ingestion.raw_collector import raw_collector
from app.metrics import ALERTS_SUPPRESSED
from app.models.incident import AlertManagerAlert, AlertManagerWebhook, Incident
from app.services.teamcity_service import incident_teamcity_context
from app.workers.tasks import (async_process_incident, celery_app,
                               process_incident_task)

router = APIRouter()
log = structlog.get_logger()


def _suppress_names() -> List[str]:
    """Объединённый allowlist: defaults из config + env extra (CSV)."""
    base = list(settings.ALERT_SUPPRESS_NAMES or [])
    extra_csv = settings.ALERT_SUPPRESS_NAMES_EXTRA or ""
    extras = [s.strip() for s in extra_csv.split(",") if s.strip()]
    # Preserve order, drop dups.
    seen = set()
    out: List[str] = []
    for n in base + extras:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _alert_in_allowlist(alertname: str, patterns: Iterable[str]) -> Optional[str]:
    """Возвращает matched-pattern если alertname попадает в allowlist.

    Используем substring-match (а не exact), чтобы один префикс
    `KubeAPIServerSlo` ловил все `*Master`/`*Node`/`*Read` варианты.
    """
    if not alertname:
        return None
    for p in patterns:
        if p and p in alertname:
            return p
    return None


def _is_self_noise(alert: AlertManagerAlert) -> bool:
    """Эвристика: alert с severity=info из service=monitoring — self-noise.

    Это покрывает алерты из monitoring-стека самого AM/Prometheus,
    которые не имеют названия в allowlist но являются техническим шумом.
    """
    labels = alert.labels or {}
    sev = (labels.get("severity") or "").lower()
    service = (labels.get("service") or "").lower()
    return sev == "info" and service == "monitoring"


def _filter_suppressed(
    alerts: List[AlertManagerAlert],
) -> tuple[List[AlertManagerAlert], int]:
    """Возвращает (passed_alerts, suppressed_count). Логирует+метрит каждый skip."""
    patterns = _suppress_names()
    passed: List[AlertManagerAlert] = []
    suppressed = 0
    for alert in alerts:
        alertname = (alert.labels or {}).get("alertname", "")
        matched = _alert_in_allowlist(alertname, patterns)
        if matched:
            ALERTS_SUPPRESSED.labels(reason="allowlist", alertname=alertname).inc()
            log.info(
                "webhook.alert_suppressed",
                reason="allowlist",
                alertname=alertname,
                matched_pattern=matched,
            )
            suppressed += 1
            continue
        if _is_self_noise(alert):
            ALERTS_SUPPRESSED.labels(reason="self_noise", alertname=alertname).inc()
            log.info(
                "webhook.alert_suppressed",
                reason="self_noise",
                alertname=alertname,
            )
            suppressed += 1
            continue
        passed.append(alert)
    return passed, suppressed


async def verify_alertmanager_signature(request: Request):
    """Verify HMAC-SHA256 signature on AlertManager webhook.

    Fail-closed: без настроенного ALERTMANAGER_WEBHOOK_SECRET все запросы
    отклоняются с 401 в ЛЮБОМ окружении. Раньше отсутствие секрета молча
    отключало проверку везде, кроме буквального ENV == "production" —
    `prod`/`staging`/`Production` принимали неаутентифицированные вебхуки.
    Для локального e2e без ключа есть ЯВНЫЙ opt-out —
    ALERTMANAGER_ALLOW_UNAUTHENTICATED=true (шумит warning-ом в логах).
    """
    if not settings.ALERTMANAGER_WEBHOOK_SECRET:
        if getattr(settings, "ALERTMANAGER_ALLOW_UNAUTHENTICATED", False):
            log.warning(
                "alertmanager.webhook_unauthenticated",
                env=settings.ENV,
                reason="explicit ALERTMANAGER_ALLOW_UNAUTHENTICATED opt-in",
            )
            return
        log.error(
            "alertmanager.webhook_rejected_no_secret",
            env=settings.ENV,
            reason="ALERTMANAGER_WEBHOOK_SECRET not set (fail-closed)",
        )
        raise HTTPException(
            status_code=401,
            detail="AlertManager webhook authentication is not configured",
        )

    signature = request.headers.get("X-Alertmanager-Signature")
    if not signature:
        raise HTTPException(status_code=401, detail="Missing AlertManager signature")

    # AlertManager-совместимый формат: либо голый hex, либо `sha256=<hex>`.
    if signature.startswith("sha256="):
        signature = signature[len("sha256="):]

    body = await request.body()

    # Anti-replay: если signer присылает X-Alertmanager-Timestamp, HMAC
    # считается над `ts.body` и проверяется окно свежести. Без заголовка —
    # backward-compatible body-only HMAC (если не включён REQUIRE_SIGNED_TIMESTAMP).
    timestamp = request.headers.get("X-Alertmanager-Timestamp")
    if timestamp:
        if not is_timestamp_fresh(
            timestamp, settings.ALERTMANAGER_WEBHOOK_MAX_AGE_SECONDS
        ):
            raise HTTPException(status_code=401, detail="Stale AlertManager timestamp")
        signed_payload = timestamp.encode() + b"." + body
    else:
        if settings.ALERTMANAGER_REQUIRE_SIGNED_TIMESTAMP:
            raise HTTPException(
                status_code=401, detail="Missing AlertManager timestamp"
            )
        signed_payload = body

    expected_signature = hmac.new(
        settings.ALERTMANAGER_WEBHOOK_SECRET.encode(), signed_payload, hashlib.sha256
    ).hexdigest()

    # Сравниваем БАЙТЫ, не str: Starlette декодирует заголовки как latin-1,
    # и не-ASCII символ в подписи ронял compare_digest(str, str) TypeError-ом
    # → 500 вместо 401. encode() тут не падает (все code points < 256).
    if not hmac.compare_digest(
        signature.encode("utf-8"), expected_signature.encode("ascii")
    ):
        raise HTTPException(status_code=401, detail="Invalid AlertManager signature")

    # Anti-replay без signing-proxy: валидно подписанный запрос принимаем
    # только один раз за окно свежести (AM повторяет уведомления не чаще
    # repeat_interval — часы, легитимные повторы в окно не попадают).
    # Покрывает и body-only путь (реальный AM), и timestamp-путь — там окно
    # свежести само по себе оставляло replay-дыру шириной в MAX_AGE_SECONDS.
    ttl = settings.ALERTMANAGER_WEBHOOK_MAX_AGE_SECONDS
    if ttl > 0 and alertmanager_signature_cache.seen_recently(signature, ttl):
        log.warning("alertmanager.webhook_replayed_signature")
        raise HTTPException(status_code=401, detail="Replayed AlertManager request")


def _resolved_duration_minutes(starts_at: str, ends_at: Optional[str]) -> Optional[int]:
    """Минуты между startsAt и endsAt алерта; None если не парсится."""
    from datetime import datetime

    try:
        s = datetime.fromisoformat((starts_at or "").replace("Z", "+00:00"))
        e = datetime.fromisoformat((ends_at or "").replace("Z", "+00:00"))
        mins = int((e - s).total_seconds() // 60)
        return mins if mins >= 0 else None
    except (ValueError, TypeError):
        return None


def validate_alert_labels(alert):
    """Validate alert labels for security."""
    labels = alert.labels
    alertname = labels.get("alertname")
    namespace = labels.get("namespace")

    if not alertname:
        raise HTTPException(status_code=400, detail="Missing alertname in labels")

    if namespace:
        # Basic validation — no special chars that could be used for injection
        if not re.match(r"^[a-z0-9-]+$", namespace):
            raise HTTPException(status_code=400, detail="Invalid namespace format")

    # instance label (Node* alerts) — same injection guard
    instance = labels.get("instance", "")
    if instance and not re.match(r"^[a-zA-Z0-9._:/-]+$", instance):
        raise HTTPException(status_code=400, detail="Invalid instance format")

    # service/app — уходят verbatim в JQL (`summary ~ "{service}"` в
    # jira_client) и в KG-атрибуцию. Консервативный charset против
    # JQL/поисковых инъекций (вторая линия — экранирование в _jql_quote).
    for label_name in ("service", "app"):
        value = labels.get(label_name, "")
        if value and not re.match(r"^[a-zA-Z0-9._-]{1,253}$", value):
            raise HTTPException(
                status_code=400, detail=f"Invalid {label_name} format"
            )


def _claim_refire(
    db: Session, incident_id: str, expected_status: str, new_data: dict
) -> int:
    """Atomically claim a re-fire (flapping / FAILED-retry) for dispatch.

    Compare-and-swap: flip the row to OPEN только если она ВСЁ ЕЩЁ в том
    терминальном/re-runnable статусе, который мы прочитали (`expected_status`).
    Два конкурентных webhook-запроса читают один и тот же статус; первый
    переводит строку в OPEN, у второго условный UPDATE матчит 0 строк →
    caller трактует это как dedup и НЕ диспатчит пайплайн (иначе — двойной
    LLM-burn + дублирующий @here).

    Это ОДИН атомарный `UPDATE ... WHERE status=:expected` + commit, поэтому
    row-lock НЕ держится через `await incident_teamcity_context(...)` (тот
    сетевой вызов уже завершился до claim'а).

    Returns rowcount: 1 = этот caller выиграл claim (диспатчим), 0 = другой
    воркер уже забрал инцидент (deduplicated).
    """
    rows = (
        db.query(IncidentRecord)
        .filter(
            IncidentRecord.incident_id == incident_id,
            IncidentRecord.status == expected_status,
        )
        .update(
            {
                IncidentRecord.status: IncidentState.OPEN.value,
                IncidentRecord.data: new_data,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return rows


def _apply_resolve(
    db: Session, existing: IncidentRecord, incident: Incident, prev_status: str
) -> str:
    """Применить resolve-вебхук к нетерминальной строке ЧЕРЕЗ state machine.

    Раньше статус писался силой из ЛЮБОГО нетерминального состояния. Для
    короткоживущего алерта (median TTR ~11 мин против многоминутного
    пайплайна) это означало: строка уезжает в RESOLVED, пока пайплайн ещё
    в полёте, и он умирает на следующем `_safe_transition` — отчёт не
    отправляется, а в analysis остаётся `resolved_early`-огрызок вместо
    нормального разбора.

    Поведение теперь:

    * переход `current → RESOLVED` ВАЛИДЕН по StateMachine
      (FACTS_COLLECTED / FIX_PROPOSED / EXECUTING) — пишем RESOLVED, как и
      раньше;
    * переход НЕВАЛИДЕН (OPEN / INVESTIGATING / HYPOTHESIS_GENERATED /
      APPROVAL_PENDING — пайплайн в середине работы) — статус НЕ трогаем.
      Резолв не теряем: кладём маркер в `data` (по образцу `flap_count`,
      который так же живёт в `data`) и логируем факт. Пайплайн доводит
      инцидент до своего терминала штатно и отправляет отчёт, а маркер
      остаётся следом для постмортема «алерт погас, пока мы его разбирали».

    Возвращает строку для поля `task_id` в ответе: "resolved" (статус
    записан) либо "resolve-deferred" (отложено до конца пайплайна).
    """
    try:
        current_state = IncidentState(prev_status)
    except ValueError:
        # Незнакомый/legacy статус — вслепую в терминал не переводим.
        current_state = None

    if current_state is not None and StateMachine.validate_transition(
        current_state, IncidentState.RESOLVED
    ):
        existing.status = IncidentState.RESOLVED.value
        db.commit()
        log.info(
            "webhook.alert_resolved",
            incident_id=incident.incident_id,
            prev_status=prev_status,
        )
        return "resolved"

    from datetime import datetime, timezone

    # data — обычный JSON-столбец без MutableDict, in-place мутацию
    # SQLAlchemy не заметит: пересобираем dict целиком. Статус в UPDATE не
    # попадает (атрибут не менялся) — гонки с пайплайном, который пишет
    # status из своей сессии, тут нет.
    existing.data = {
        **(existing.data or {}),
        "resolve_pending": True,
        "resolve_pending_from": prev_status,
        "resolved_at": incident.ends_at or datetime.now(timezone.utc).isoformat(),
    }
    db.commit()
    log.info(
        "webhook.alert_resolve_deferred",
        incident_id=incident.incident_id,
        prev_status=prev_status,
        reason="transition_invalid_pipeline_in_flight",
    )
    return "resolve-deferred"


@router.post(
    "/alertmanager",
    status_code=202,
    dependencies=[Depends(verify_alertmanager_signature)],
)
async def alertmanager_webhook(
    payload: AlertManagerWebhook, db: Session = Depends(get_db)
):
    """Receive a Prometheus AlertManager webhook batch and dispatch one Celery task per alert."""
    # raw_collector requires `id` or `incident_id` at top level. AlertManager
    # batch не имеет глобального id, поэтому используем `groupKey` как идентификатор
    # этого batch-а (один webhook = один batch с одним groupKey).
    raw_payload = payload.model_dump()
    raw_payload.setdefault("id", payload.groupKey)
    raw_collector.ingest(raw_payload)

    # A3: allowlist filter. Применяем сразу после raw-store, чтобы суrnu trace
    # сохранил исходный batch, но дальше идут только не-noise alerts.
    payload_alerts, _suppressed = _filter_suppressed(payload.alerts)

    # States where the pipeline is already in-flight — skip re-dispatch.
    # FAILED is the only terminal state we allow to re-run (transient infra error).
    # NOTE: RESOLVED и TRIAGE_REQUIRED намеренно отсутствуют — re-fire после
    # любого из них является flapping и должен быть переобработан (см. flapping
    # detection ниже). TRIAGE_REQUIRED — это «unresolved» терминал, его re-fire
    # обрабатываем так же, как re-fire после RESOLVED.
    _SKIP_STATES = {
        IncidentState.OPEN.value,
        IncidentState.INVESTIGATING.value,
        IncidentState.FACTS_COLLECTED.value,
        IncidentState.HYPOTHESIS_GENERATED.value,
        IncidentState.FIX_PROPOSED.value,
    }

    accepted = []
    for alert in payload_alerts:
        try:
            validate_alert_labels(alert)
        except HTTPException:
            # Один битый alert не должен ронять весь batch (паттерн /store):
            # иначе уже принятые alerts закоммичены, остальные дропнуты, а
            # AM-retry вечно бьётся в тот же битый alert.
            log.warning("webhook.skipped_invalid_alert", labels=alert.labels)
            continue
        incident = Incident.from_alertmanager(alert)

        existing = (
            db.query(IncidentRecord)
            .filter(IncidentRecord.incident_id == incident.incident_id)
            .first()
        )

        # ── RESOLVED webhook: update DB, no pipeline dispatch ──────────────
        if alert.status == "resolved":
            resolve_result = "resolved"
            if existing is not None and existing.status not in {
                IncidentState.RESOLVED.value,
                IncidentState.TRIAGE_REQUIRED.value,
                IncidentState.FAILED.value,
            }:
                # prev_status ЧИТАЕМ ДО присваивания. Раньше лог стоял ПОСЛЕ
                # `existing.status = RESOLVED` и печатал уже НОВОЕ значение —
                # всегда "RESOLVED". Постмортем терял единственный след того,
                # из какого состояния инцидент был закрыт резолвом.
                prev_status = str(existing.status)
                resolve_result = _apply_resolve(db, existing, incident, prev_status)
            accepted.append({
                "incident_id": incident.incident_id,
                "task_id": resolve_result,
            })
            continue

        # ── FIRING: detect flapping (re-fire after RESOLVED/TRIAGE_REQUIRED) ─
        # TRIAGE_REQUIRED — терминал для неразрешённых инцидентов; его re-fire
        # тоже flapping и переобрабатывается как re-fire после RESOLVED.
        if existing is not None and existing.status in {
            IncidentState.RESOLVED.value,
            IncidentState.TRIAGE_REQUIRED.value,
        }:
            prev_flap_count = (existing.data or {}).get("flap_count", 0)
            incident = incident.model_copy(update={"flap_count": prev_flap_count + 1})
            log.info(
                "webhook.flapping_detected",
                incident_id=incident.incident_id,
                flap_count=incident.flap_count,
            )
            # Fall through to TC enrichment + pipeline re-run.

        # ── Normal dedup: pipeline already in-flight ───────────────────────
        elif existing is not None and existing.status in _SKIP_STATES:
            log.info(
                "webhook.deduplicated",
                incident_id=incident.incident_id,
                status=existing.status,
            )
            accepted.append({
                "incident_id": incident.incident_id,
                "task_id": "deduplicated",
                "status": existing.status,
            })
            continue

        # TC enrichment — best-effort, only for new / retriable incidents.
        try:
            incident.teamcity_context = await incident_teamcity_context(
                namespace=incident.namespace,
                incident_starts_at=incident.starts_at,
            )
        except Exception as e:
            log.warning(
                "teamcity_context.unhandled",
                error=str(e),
                incident_id=incident.incident_id,
            )

        if existing is None:
            # New incident — INSERT. Commit before pipeline: the worker opens
            # its own SessionLocal and needs the row visible before it starts
            # writing state transitions.
            db.add(
                IncidentRecord(
                    incident_id=incident.incident_id,
                    status=IncidentState.OPEN.value,
                    data=incident.model_dump(),
                )
            )
            try:
                db.commit()
            except IntegrityError:
                # Конкурентный дубль: другой воркер уже создал строку с этим
                # incident_id (UNIQUE). Откатываемся и трактуем как dedup —
                # пайплайн уже запущен тем воркером, второй dispatch не нужен.
                db.rollback()
                log.info(
                    "webhook.insert_race_deduplicated",
                    incident_id=incident.incident_id,
                )
                accepted.append({
                    "incident_id": incident.incident_id,
                    "task_id": "deduplicated",
                })
                continue
        else:
            # Re-fire (FAILED retry / flapping): atomic compare-and-swap claim.
            # Reset to OPEN только если строка ВСЁ ЕЩЁ в наблюдаемом статусе —
            # так ровно ОДИН из двух конкурентных webhook'ов выигрывает claim и
            # диспатчит, второй получает rowcount 0 и дедупится (без двойного
            # LLM-burn / дубль-@here). data перезаписывается, чтобы flap_count
            # сохранился в строке.
            claimed = _claim_refire(
                db,
                incident.incident_id,
                str(existing.status),
                incident.model_dump(),
            )
            if claimed == 0:
                log.info(
                    "webhook.refire_race_deduplicated",
                    incident_id=incident.incident_id,
                )
                accepted.append({
                    "incident_id": incident.incident_id,
                    "task_id": "deduplicated",
                })
                continue

        if settings.PIPELINE_DIRECT_INVOKE:
            await async_process_incident(incident.model_dump())
            accepted.append({"incident_id": incident.incident_id, "task_id": "direct"})
        else:
            task = process_incident_task.delay(incident.model_dump())
            accepted.append({"incident_id": incident.incident_id, "task_id": task.id})

    return {"status": "accepted", "alerts": accepted}


@router.post(
    "/alertmanager/store",
    status_code=202,
    dependencies=[Depends(verify_alertmanager_signature)],
)
async def alertmanager_webhook_store_only(
    payload: AlertManagerWebhook, db: Session = Depends(get_db)
):
    """KG event-store endpoint — БЕЗ LLM-pipeline'а.

    Принимает AlertManager batch, записывает каждый alert в `kg_alerts`
    через `populate_from_incident`, ACK 202. Никакого
    `process_incident_task.delay()` — LLM-токены НЕ расходуются.

    Цель: наполнить event-store чтобы `nearby_alerts`/`incidents_on`/
    recurrence-detection заработали на live data. AlertManager рулится
    сюда; основной `/alertmanager` endpoint остаётся отключенным до
    запуска E2E-тестов и budget-cap'ов.

    HMAC-подпись и signature-check те же что и у full pipeline endpoint.
    """
    from app.knowledge_graph.auto_populator import populate_from_incident

    raw_payload = payload.model_dump()
    raw_payload.setdefault("id", payload.groupKey)
    raw_collector.ingest(raw_payload)

    # A3: allowlist filter — pure-noise alerts даже в KG-store не пишем,
    # чтобы recurrence/incidents_on метрики не были загажены Watchdog'ом.
    payload_alerts, suppressed_count = _filter_suppressed(payload.alerts)

    stored = []
    for alert in payload_alerts:
        try:
            validate_alert_labels(alert)
            incident = Incident.from_alertmanager(alert)
        except HTTPException:
            # Малформированные alerts skip-аем, не падаем на batch.
            log.warning("kg_store.skipped_invalid_alert", labels=alert.labels)
            continue

        # Resolved-events тоже регистрируем — даёт recurrence/flapping
        # сигнал, не только firing.
        if alert.status == "resolved":
            stored.append({"incident_id": incident.incident_id, "result": "resolved-skipped"})
            continue

        try:
            stats = populate_from_incident(db, incident)
            stored.append({"incident_id": incident.incident_id, "result": "stored", **stats})
        except Exception as e:
            # Откатываем failed-транзакцию, иначе session остаётся в
            # сорванном состоянии и финальный db.commit() уронит весь batch.
            db.rollback()
            log.warning(
                "kg_store.populate_failed",
                incident_id=incident.incident_id,
                error=type(e).__name__,
                message=str(e),
            )
            stored.append({"incident_id": incident.incident_id, "result": "failed"})

    db.commit()
    return {"status": "stored", "alerts": stored, "suppressed_allowlist": suppressed_count}


@router.post(
    "/alertmanager/enrich-and-forward",
    status_code=202,
    dependencies=[Depends(verify_alertmanager_signature)],
)
async def alertmanager_webhook_enrich_and_forward(
    payload: AlertManagerWebhook, db: Session = Depends(get_db)
):
    """KG-enriched Discord-forward, БЕЗ LLM.

    Делает то же, что /store (пишет alert-event в kg_alerts), и
    дополнительно для каждого FIRING alert-а собирает KG-контекст
    (recent_deploys, nearby_alerts, recurrence, downstream, owner) и
    отправляет один embed в Discord webhook.

    Группировка: alerts с одинаковым (alertname, severity) в одном
    AM-batch сворачиваются в один embed (несколько ns в одном сообщении).
    Это снижает шум типа «3 одинаковых KubePodCrashLooping подряд».

    Если DISCORD_ENRICH_ENABLED=false — поведение идентично /store.
    """
    from app.knowledge_graph.auto_populator import populate_from_incident
    from app.services.alert_enrichment import enrich_alert_async
    from app.services.discord_service import DiscordService

    raw_payload = payload.model_dump()
    raw_payload.setdefault("id", payload.groupKey)
    raw_collector.ingest(raw_payload)

    # A3: allowlist filter — Watchdog/InfoInhibitor и self-noise отсеиваем
    # ДО enrichment-стадии. Это снимает ~30-40% бесполезного шума.
    payload_alerts, suppressed_allowlist = _filter_suppressed(payload.alerts)

    stored = []
    firing_incidents = []  # (incident, env-hint) — для post-store enrich.
    resolved_criticals = []  # critical-резолвы → зелёный notice в Discord.

    for alert in payload_alerts:
        try:
            validate_alert_labels(alert)
            incident = Incident.from_alertmanager(alert)
        except HTTPException:
            log.warning("enrich_forward.skipped_invalid_alert", labels=alert.labels)
            continue

        if alert.status == "resolved":
            # Warning-резолвы в Discord не идут (шум); critical — короткий
            # зелёный notice, чтобы был виден конец инцидента.
            stored.append({"incident_id": incident.incident_id, "result": "resolved-skipped"})
            if (incident.severity or "").lower() == "critical":
                resolved_criticals.append(incident)
            continue

        try:
            stats = populate_from_incident(db, incident)
            stored.append({"incident_id": incident.incident_id, "result": "stored", **stats})
        except Exception as e:
            # Откатываем failed-транзакцию, иначе session остаётся в
            # сорванном состоянии и финальный db.commit() уронит весь batch.
            db.rollback()
            log.warning(
                "enrich_forward.populate_failed",
                incident_id=incident.incident_id,
                error=type(e).__name__,
                message=str(e),
            )
            stored.append({"incident_id": incident.incident_id, "result": "failed"})
        firing_incidents.append(incident)

    db.commit()

    # Discord-enrich tier. Под фичефлагом — чтобы /store-style behaviour
    # сохранялся, пока канарейка не подтвердит безвредность.
    enriched_groups = 0
    suppressed_chronic = 0
    suppressed_rollout = 0
    suppressed_inhibited = 0
    if settings.DISCORD_ENRICH_ENABLED and firing_incidents:
        from app.services.alert_dedup import (Decision, decide_send,
                                              rollback_undelivered)
        from app.services.alert_enrichment import (_inhibition_state,
                                                   resolve_store_service)

        # Группировка по (alertname, severity) — несколько ns в одном embed.
        groups: dict[tuple, list] = {}
        for inc in firing_incidents:
            key = (
                inc.labels.get("alertname", "unknown"),
                (inc.severity or "unknown").lower(),
            )
            groups.setdefault(key, []).append(inc)

        # Hint про env берём из namespace prefix первого incident-а.
        def _env_hint(ns: Optional[str]) -> Optional[str]:
            if not ns:
                return None
            for p in ("prod", "preprod", "preupdate", "squad", "dev"):
                if ns.startswith(p + "-"):
                    return p
            return None

        discord_service = DiscordService()
        for (alertname, sev), incs in groups.items():
            # Нужны в except-ветке для отката tentative-инкремента дедупа —
            # инициализируем ДО try, иначе на падении до decide_send они
            # unbound.
            decision: Optional[Decision] = None
            head_service: Optional[str] = None
            try:
                # Дедуп решается per первой incident-у группы (один service —
                # один state-ключ; namespace-агрегацию уже сделал AM group_by).
                head_inc = incs[0]
                # STORE-путь атрибуции: у kube-resource-алертов лейбл `service`
                # = метрика-источник (vm-kube-state-metrics), не target. Держим
                # dedup/suppress-ключ на target-workload'е (как в kg_alerts),
                # иначе gen/replicas-mismatch схлопываются на один KSM-ключ.
                head_service = resolve_store_service(
                    head_inc.labels,
                    legacy_default=(
                        head_inc.labels.get("service")
                        or head_inc.labels.get("deployment")
                    ),
                )

                # A1: AM inhibit/silence gate. Если AM payload пришёл
                # с status: {state: suppressed, silencedBy/inhibitedBy: [...]},
                # для non-critical алертов skip-аем embed (AM уже принял
                # решение что это шум). Critical — всё равно шлём, но в
                # embed-е появится Status-поле + orange color.
                inhib = _inhibition_state(head_inc)
                if inhib and sev != "critical":
                    suppressed_inhibited += 1
                    ALERTS_SUPPRESSED.labels(
                        reason="inhibited_warn", alertname=alertname,
                    ).inc()
                    log.info(
                        "enrich_forward.suppress_inhibited",
                        alertname=alertname,
                        service=head_service,
                        severity=sev,
                        inhibition=inhib,
                    )
                    continue

                decision = await decide_send(
                    alertname=alertname,
                    namespace=head_inc.namespace,
                    service=head_service,
                    severity=sev,
                    db=db,
                )
                if decision == Decision.SUPPRESS_CHRONIC:
                    suppressed_chronic += 1
                    log.info(
                        "enrich_forward.suppress_chronic",
                        alertname=alertname, service=head_service,
                    )
                    continue
                if decision == Decision.SUPPRESS_ROLLOUT:
                    suppressed_rollout += 1
                    log.info(
                        "enrich_forward.suppress_rollout",
                        alertname=alertname, service=head_service,
                    )
                    continue

                # enrich_alert целиком синхронный и тяжёлый (10+ sync SQL,
                # sync Jira HTTP, live k8s API до 3s, statics-Postgres с
                # time.sleep-backoff — worst case ~15-17s). Прямой вызов
                # блокировал event loop, и на alert-storm-е вставал весь
                # API-процесс, включая health-пробы. Обёртка уводит работу
                # в thread pool.
                # Await-ы ПОСЛЕДОВАТЕЛЬНЫЕ (не gather): SQLAlchemy Session
                # не потокобезопасна, конкурентные вызовы с одной и той же
                # `db` рвут сессию.
                ctxs = [await enrich_alert_async(db, inc) for inc in incs]
                env_hint = _env_hint(incs[0].namespace)
                resurfaced = (decision == Decision.SEND_RESURFACED)
                delivered = await discord_service.send_enriched_alert(
                    ctxs, env=env_hint, resurfaced=resurfaced,
                )
                # Фаза подтверждения дедупа. decide_send уже нарастил
                # chronic-счётчик (tentative) — если embed не ушёл, счётчик
                # надо откатить, иначе три подряд неудачные доставки глушат
                # четвёртый, уже успешный fire на всё 6h-окно.
                # send_enriched_alert доставку пока не возвращает (None) —
                # трактуем None как «ушло, исключения не было»; явный False
                # (контракт send_incident_report/send_stats_report) — как
                # недоставку.
                if delivered is False:
                    log.warning(
                        "enrich_forward.send_not_delivered",
                        alertname=alertname,
                        severity=sev,
                        service=head_service,
                    )
                    await rollback_undelivered(alertname, head_service, decision)
                    continue
                enriched_groups += 1
            except Exception as e:
                # Счётчик подавления обязан считать ФАКТИЧЕСКИ отправленные
                # embed-ы: снимаем tentative-инкремент этого fire-а.
                if decision is not None:
                    await rollback_undelivered(alertname, head_service, decision)
                log.warning(
                    "enrich_forward.send_failed",
                    alertname=alertname,
                    severity=sev,
                    error=type(e).__name__,
                    message=str(e),
                )

    # Зелёные notice по critical-резолвам — после firing-блока, чтобы при
    # смешанном batch (новый fire + старый resolve) порядок был red → green.
    resolved_posted = 0
    if settings.DISCORD_ENRICH_ENABLED and resolved_criticals:
        from app.services.alert_enrichment import resolve_store_service
        from app.services.discord_service import DiscordService

        notice_service = DiscordService()
        for inc in resolved_criticals:
            try:
                await notice_service.send_resolved_notice(
                    alertname=inc.labels.get("alertname", "unknown"),
                    namespace=inc.namespace,
                    # Тот же target-резолв, что в firing-notice/kg_alerts —
                    # иначе зелёный resolve уходит на vm-kube-state-metrics.
                    service=resolve_store_service(
                        inc.labels,
                        legacy_default=(
                            inc.labels.get("service") or inc.labels.get("deployment")
                        ),
                    ),
                    duration_min=_resolved_duration_minutes(inc.starts_at, inc.ends_at),
                )
                resolved_posted += 1
            except Exception as e:
                log.warning(
                    "enrich_forward.resolved_notice_failed",
                    alertname=inc.labels.get("alertname"),
                    error=type(e).__name__,
                    message=str(e),
                )

    return {
        "status": "stored-and-forwarded",
        "alerts": stored,
        "enriched_groups": enriched_groups,
        "resolved_posted": resolved_posted,
        "suppressed_chronic": suppressed_chronic,
        "suppressed_rollout": suppressed_rollout,
        "suppressed_inhibited": suppressed_inhibited,
        "suppressed_allowlist": suppressed_allowlist,
        "enrich_enabled": settings.DISCORD_ENRICH_ENABLED,
    }


@router.get("/status/{task_id}")
async def get_task_status(task_id: str, user: User = Depends(get_current_user)):
    # Auth как у /jobs/{task_id} в main.py: без него это был анонимный
    # probe статусов Celery-задач (enumeration + утечка прогресса пайплайна).
    res = celery_app.AsyncResult(task_id)
    return {"task_id": task_id, "status": res.status}
