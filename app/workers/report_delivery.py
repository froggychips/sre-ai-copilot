"""Outbox доставки Discord-отчёта — вынесен из IncidentPipeline.

Почему отдельным модулем: доставка не является стадией разбора. Она живёт
ПОСЛЕ коммита терминального state, переживает смерть воркера, ретраится
Celery-ом и не имеет доступа ни к одной LLM-стадии. В IncidentPipeline её
двенадцать методов соседствовали с машиной состояний и обогащением, и любой
тест доставки был обязан сначала собрать весь пайплайн с моками восьми
агентов. Здесь тот же механизм тестируется на record + db.

Механика outbox-маркеров в `record.analysis` (переживают мерж блоба в
`_persist` так же, как executor_applied — `_persist` мержит, а не заменяет):

  report_pending = {"args": {...готовые поля embed...}, "attempts": N,
                    "queued_at": iso, "last_error_at": iso}
      «терминальный state закоммичен, отчёт ещё НЕ доставлен». Пишется в ТОЙ
      ЖЕ транзакции, что и терминальный переход, поэтому «статус есть, а
      маркера нет» невозможно.
  report_sent   = {"sent_at": iso, "attempts": N} — доставлено, pending снят.
  report_failed = {"failed_at": iso, "attempts": N, "reason": ...} — сдались
      после MAX_ATTEMPTS, чтобы ретраи не крутились на мёртвом вебхуке вечно.

Два сценария, от которых всё это защищает (оба были живыми багами):
  (а) транзиентный фейл POST-а — таск завершался «успешно», отчёта нет;
  (б) смерть воркера в окне «commit → send → ack» (acks_late) — redelivery
      видела терминальный start_state и уходила в resolved_early.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

import structlog

from app.config import settings
from app.core.execution_dsl import ExecutionIntent
from app.services.audit_logger import audit_service
from app.services.discord_service import discord_service

logger = structlog.get_logger()

__all__ = [
    "ReportDelivery",
    "ReportDeliveryPending",
    "severity_routeable",
]


class ReportDeliveryPending(ConnectionError):
    """Discord-отчёт не доставлен; терминальный state УЖЕ закоммичен.

    НАМЕРЕННО наследует ConnectionError: RETRIABLE_EXC в app/workers/tasks.py
    (тот же кортеж читает Celery `autoretry_for`) содержит ConnectionError,
    поэтому таск ретраится, а следующая попытка видит outbox-маркер
    report_pending и досылает ТОЛЬКО отчёт — без повторного прожига LLM-стадий.
    Бросается строго ПОСЛЕ коммита терминального state: статус инцидента
    ретраем не откатывается, ретраится ровно доставка.
    """


def severity_routeable(severity: Optional[str]) -> bool:
    """Уйдёт ли отчёт в канал вообще (severity-gate discord-сервиса).

    info/none/пустой severity сервис в #infra-error НЕ шлёт и по контракту
    отдаёт delivered=False. Это не потеря отчёта, поэтому такой False не должен
    поднимать ReportDeliveryPending. Читаем ровно тот же helper, что и сервис —
    чтобы правило не разъехалось с ним. Недоступен → считаем routeable
    (лучше лишний ретрай, чем молча похоронённый разбор).
    """
    try:
        from app.services.discord.routing import _should_route_to_error
        return bool(_should_route_to_error(severity or ""))
    except (ImportError, AttributeError):
        # Ровно два случая, когда «не смогли спросить сервис»: модуль не
        # импортируется либо helper переименовали. Широкий `except Exception`
        # здесь был опасен в другую сторону: он бы проглотил и NameError с
        # TypeError из самого helper-а, то есть после рефакторинга severity-gate
        # молча превратился бы в «ретраить всё», включая info-алерты.
        return True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReportDelivery:
    """Доставка одного инцидентного отчёта с ретраем через outbox-маркер.

    Владеет: маркерами в `record.analysis`, счётчиком попыток, решением
    «ретраить или сдаться». НЕ владеет: содержимым embed-а — args приходят
    готовыми от пайплайна (он один знает, что показать человеку).

    Жизненный цикл на прогон:
        rd = ReportDelivery(incident_id, db, record)
        marker = rd.new_marker(args)   # кладётся в analysis транзакцией _persist
        rd.mark_outboxed()             # маркер закоммичен вместе со state
        rd.stage(enriched_args)        # in-memory копия с полями embed
        await rd.flush()               # попытка доставки
    Повторный прогон терминальной строки:
        pending = rd.load_pending()
        if pending: await rd.resend(pending)
    """

    PENDING_KEY = "report_pending"
    SENT_KEY = "report_sent"
    FAILED_KEY = "report_failed"

    # Сколько попыток доставки суммарно (первая + Celery-ретраи). 4 = 1 +
    # max_retries у process_incident_task: на последней пишем report_failed и
    # НЕ бросаем, чтобы не тратить лишний прогон таска. getattr — чтобы не
    # трогать config.py.
    MAX_ATTEMPTS = int(getattr(settings, "REPORT_DELIVERY_MAX_ATTEMPTS", 4) or 4)

    def __init__(self, incident_id: str, db, record) -> None:
        self.incident_id = incident_id
        self.db = db
        self.record = record
        # in-memory копия маркера + признак того, что он реально закоммичен в
        # БД (только тогда ретрай сможет дослать отчёт без LLM-стадий).
        self.pending: Optional[Dict[str, Any]] = None
        self.outboxed: bool = False

    # ------------------------------------------------------------------
    # Маркеры
    # ------------------------------------------------------------------

    def new_marker(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Свежий report_pending для вставки в analysis ВНУТРИ транзакции _persist."""
        return {"args": args, "attempts": 0, "queued_at": _now_iso()}

    def mark_outboxed(self) -> None:
        """Маркер закоммичен вместе с терминальным state — ретрай теперь возможен.

        Пока флаг не выставлен, недоставленный отчёт НЕ ретраится: без строки
        в БД (ad-hoc прогон) переотправка прогнала бы LLM-стадии заново, что
        дороже потерянного embed-а.
        """
        self.outboxed = True

    def stage(self, args: Dict[str, Any]) -> None:
        """In-memory копия маркера с финальными полями embed (после обогащения)."""
        self.pending = {"args": args, "attempts": 0}

    def load_pending(self) -> Optional[Dict[str, Any]]:
        """Живой outbox-маркер (отчёт не доставлен) или None."""
        if self.record is None:
            return None
        analysis = self.record.analysis
        if not isinstance(analysis, dict):
            return None
        if analysis.get(self.SENT_KEY) or analysis.get(self.FAILED_KEY):
            # Доставлено либо сдались — pending-хвост не воскрешаем.
            return None
        pending = analysis.get(self.PENDING_KEY)
        if not isinstance(pending, dict) or not isinstance(pending.get("args"), dict):
            return None
        return pending

    def _lock_record(self) -> None:
        """Взять row-lock на строку инцидента и перечитать её (SELECT … FOR UPDATE).

        Без лока запись маркера — классический read-modify-write по JSON-колонке:
        прочитали `analysis` целиком, дописали свой ключ, записали целиком.
        Второй писатель, прочитавший ту же версию, затирает работу первого — и
        теряется не «лишний» ключ, а, например, `executor_applied`, который
        служит единственным guard-ом от повторного реального kubectl.

        Параллельный писатель здесь реален: при `acks_late` + redis-брокере
        задача переотправляется по visibility timeout, и redelivery может
        начать досылку отчёта, пока прежний воркер ещё дописывает маркер.

        Тот же приём и по тем же причинам применён в `executor_apply.py:245` и
        в `discord/dedup_store.py`; здесь он до сих пор отсутствовал.

        Именно `refresh`, а не отдельный SELECT в новый объект: перечитывание в
        новую переменную уже дало дефект — на сессии-моке запрос возвращал не ту
        строку, и маркер уезжал в пустой `analysis`, затирая `executor_applied`
        (поймано `test_failed_delivery_marker_survives_analysis_merge`). refresh
        обновляет ТОТ ЖЕ объект, поэтому identity map остаётся согласованной, а
        вызывающий продолжает работать с той же записью.

        Побочный и главный эффект: под локом мы видим свежую версию `analysis`,
        то есть чужие ключи, дописанные параллельным писателем, попадают в нашу
        копию, а не затираются ею.

        Лок держится до commit/rollback в `_update_analysis`. На SQLite (тесты)
        `FOR UPDATE` — no-op, поведение не меняется. Любая ошибка здесь
        проглатывается: маркер важнее лока, и терять его из-за недоступного
        `SELECT … FOR UPDATE` нельзя.
        """
        if self.record is None:
            return
        try:
            self.db.refresh(self.record, with_for_update=True)
        except Exception as e:  # noqa: BLE001 — лок best-effort, маркер важнее
            logger.debug(
                "pipeline.report_marker_lock_skipped",
                incident_id=self.incident_id,
                error=type(e).__name__,
            )

    def _update_analysis(
        self, mutate: Callable[[Dict[str, Any]], None], marker: str
    ) -> None:
        """Точечно поправить record.analysis под row-lock и закоммитить.

        Best-effort: маркер — служебная запись поверх уже закоммиченного
        терминального состояния, и её неудача не должна ронять разбор.
        """
        if self.record is None:
            return
        try:
            self._lock_record()
            current = self.record.analysis
            analysis = dict(current) if isinstance(current, dict) else {}
            mutate(analysis)
            self.record.analysis = analysis
            self.db.commit()
        except Exception as e:
            logger.warning(
                "pipeline.report_marker_write_failed",
                incident_id=self.incident_id,
                marker=marker,
                error=str(e),
            )
            try:
                self.db.rollback()
            except Exception:
                pass

    def mark_sent(self, attempts: int) -> None:
        def _mutate(analysis: Dict[str, Any]) -> None:
            analysis.pop(self.PENDING_KEY, None)
            analysis[self.SENT_KEY] = {"sent_at": _now_iso(), "attempts": attempts}

        self.pending = None
        self._update_analysis(_mutate, self.SENT_KEY)

    def mark_failed(self, attempts: int, reason: str) -> None:
        def _mutate(analysis: Dict[str, Any]) -> None:
            analysis.pop(self.PENDING_KEY, None)
            analysis[self.FAILED_KEY] = {
                "failed_at": _now_iso(),
                "attempts": attempts,
                "reason": reason,
            }

        self.pending = None
        self._update_analysis(_mutate, self.FAILED_KEY)

    def bump_attempts(self, args: Dict[str, Any], attempts: int) -> None:
        """Зафиксировать номер попытки в маркере ДО броска ReportDeliveryPending."""
        now_iso = _now_iso()

        def _mutate(analysis: Dict[str, Any]) -> None:
            pending = analysis.get(self.PENDING_KEY)
            base = dict(pending) if isinstance(pending, dict) else {"queued_at": now_iso}
            base["args"] = args
            base["attempts"] = attempts
            base["last_error_at"] = now_iso
            analysis[self.PENDING_KEY] = base

        if self.pending is not None:
            self.pending["attempts"] = attempts
        self._update_analysis(_mutate, self.PENDING_KEY)

    def refresh_args(self, args: Dict[str, Any]) -> None:
        """Досыпать обогащённые поля в уже закоммиченный маркер (best-effort)."""
        def _mutate(analysis: Dict[str, Any]) -> None:
            pending = analysis.get(self.PENDING_KEY)
            base = (
                dict(pending)
                if isinstance(pending, dict)
                else {"attempts": 0, "queued_at": _now_iso()}
            )
            base["args"] = args
            analysis[self.PENDING_KEY] = base

        self._update_analysis(_mutate, self.PENDING_KEY)

    # ------------------------------------------------------------------
    # Отправка
    # ------------------------------------------------------------------

    @staticmethod
    def send_kwargs(args: Dict[str, Any]) -> Dict[str, Any]:
        """Развернуть сериализованные поля маркера в kwargs discord-сервиса."""
        kwargs = dict(args)
        intent_raw = kwargs.get("execution_intent")
        if isinstance(intent_raw, dict):
            try:
                kwargs["execution_intent"] = ExecutionIntent.model_validate(intent_raw)
            except Exception:
                # Битый intent не должен стоить нам всего отчёта — шлём без него
                # (без intent-а просто не будет apply/approve-кнопок).
                kwargs["execution_intent"] = None
        ts_raw = kwargs.get("incident_ts")
        if isinstance(ts_raw, str):
            try:
                kwargs["incident_ts"] = datetime.fromisoformat(ts_raw)
            except ValueError:
                kwargs["incident_ts"] = None
        return kwargs

    async def send_once(self, args: Dict[str, Any]) -> bool:
        """Одна попытка доставки. Возвращает delivered, наружу не бросает.

        Контракт discord-сервиса: send_incident_report отдаёт bool delivered
        и сам не бросает. Явный False = «не доставлено» → ретрай по outbox-у.
        Возврат None трактуем как доставку: так ведёт себя старый контракт
        (метод ничего не возвращал), а ретраить вслепую хуже, чем поверить.
        """
        try:
            delivered = await discord_service.send_incident_report(
                **self.send_kwargs(args)
            )
        except Exception as e:
            # Defensive: по контракту сюда не приходим, но проглоченный отчёт
            # без маркера хуже, чем лишний ретрай.
            logger.warning(
                "pipeline.report_send_raised",
                incident_id=self.incident_id,
                error=type(e).__name__,
            )
            return False
        return delivered is not False

    async def flush(self) -> None:
        """Доставить отчёт этого прогона (вызывается из run() после synthesize)."""
        if self.pending is None:
            return
        await self.deliver(self.pending)

    async def resend(self, pending: Dict[str, Any]) -> None:
        """Повторная отправка по живому outbox-маркеру. LLM-стадии НЕ трогаем.

        Дубля не будет: дедуп Discord-а живёт в таблице discord_dedup и
        клеймится ДО POST-а (claim/release в app/services/discord/dedup_store.py).
        Провалившийся POST claim отпускает — повторная попытка честно постит
        заново. Если воркер умер между claim и POST-ом, повтор увидит
        placeholder без msg_id и промолчит (дубля не создаст), а после TTL
        отправит заново; от вечного круга страхует MAX_ATTEMPTS.
        """
        audit_service.log_event(
            "PIPELINE_REPORT_RESEND",
            {
                "incident_id": self.incident_id,
                "attempts_done": int(pending.get("attempts") or 0),
            },
        )
        self.outboxed = True
        self.pending = pending
        await self.deliver(pending)

    async def deliver(self, pending: Dict[str, Any]) -> None:
        """Попытка + решение о ретрае. Бросает ReportDeliveryPending для Celery."""
        args = pending.get("args")
        if not isinstance(args, dict):
            logger.error(
                "pipeline.report_marker_unusable", incident_id=self.incident_id
            )
            self.mark_failed(int(pending.get("attempts") or 0), "marker_unusable")
            return
        attempts = int(pending.get("attempts") or 0) + 1
        if await self.send_once(args):
            self.mark_sent(attempts)
            return
        if not self.outboxed:
            # Строки инцидента нет (ad-hoc прогон) — переотправлять не с чего:
            # ретрай прогонит LLM-стадии заново, что дороже потерянного embed-а.
            logger.error(
                "pipeline.report_delivery_lost_no_record",
                incident_id=self.incident_id,
            )
            return
        if not severity_routeable(args.get("severity")):
            # Не сбой доставки: discord-сервис намеренно не шлёт info/none в
            # #infra-error (severity-gate) и отдаёт False. Ретраить нечего.
            logger.info(
                "pipeline.report_delivery_skipped_low_severity",
                incident_id=self.incident_id,
                severity=args.get("severity"),
            )
            self.mark_failed(attempts, "severity_gate_skip")
            return
        if attempts >= self.MAX_ATTEMPTS:
            logger.error(
                "pipeline.report_delivery_gave_up",
                incident_id=self.incident_id,
                attempts=attempts,
            )
            audit_service.log_event(
                "PIPELINE_REPORT_DELIVERY_FAILED",
                {"incident_id": self.incident_id, "attempts": attempts},
            )
            self.mark_failed(attempts, "discord_delivery_failed")
            return
        self.bump_attempts(args, attempts)
        raise ReportDeliveryPending(
            f"incident {self.incident_id}: Discord report not delivered "
            f"(attempt {attempts}/{self.MAX_ATTEMPTS}); terminal state is "
            f"committed, {self.PENDING_KEY} kept for redelivery"
        )
