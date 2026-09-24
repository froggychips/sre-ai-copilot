"""Единый контракт сборщиков контекста инцидента: результат → source_status.

До этого модуля каждый сборщик в `enrich_alert` и в `stage_diagnose` был
своим try/except, и Known Unknowns (`source_status`, см.
`app/diagnostics/rules/base.py`) дописывались руками по месту: где-то
причина ставилась, где-то забывалась. Так пайплайн годами клал в
`logs_summary` строку «[k8s_facts unavailable: …]» с пустыми
`k8s_events`, и правило отвечало ✗ «OOM не было» с confidence 0.95 —
уверенный вердикт из недоступного API.

Здесь одно правило для всех: сборщик отдаёт `CollectorResult` со статусом
из того же словаря, что и задачи-источники графа
(`app/knowledge_graph/source_status.SourceStatus`), и запись в
`source_status` ВЫВОДИТСЯ из статуса, а не пишется вручную:

  * SUCCESS / PARTIAL — данные есть, записи нет;
  * EMPTY             — источник ответил пустотой, записи нет: «опрошено,
                        пусто» и есть законный ABSENT;
  * UNAVAILABLE / FAILED / INVALID — запись для каждого поля из
                        `ctx_fields` с причиной; правила ответят ?, а не ✗.

Почему PARTIAL без записи. PARTIAL — «ответил не весь», но то, что пришло,
наблюдаемо: FOUND по нему честен, а ABSENT… тоже, пока сборщик сам не
решит, что неполнота делает пустоту недоказуемой — тогда он отдаёт
UNAVAILABLE с причиной (так устроен «поток деплоев не пополняется»).

LLM tool-calling сюда не относится и не появится: сборщики по-прежнему
запускаются кодом ДО промпта, модель ни один из них не выбирает.

Таймаут. У асинхронного прогона (`Collector.run`) дедлайн держит
`asyncio.wait_for`: истёк — UNAVAILABLE с причиной. Синхронные сборщики
(`Collector.run_sync`) ходят в БД и в k8s API из пула потоков enrichment-а,
прервать их снаружи нельзя; дедлайн у них — внутри клиента
(`_request_timeout` k8s, statement timeout БД), поэтому `timeout_seconds`
для них только декларация, а не гарантия.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (Any, Awaitable, Callable, Dict, MutableMapping, Optional,
                    Tuple)

import structlog

from app.knowledge_graph.source_status import SourceStatus

__all__ = [
    "Collector",
    "CollectorResult",
    "Outcome",
    "PROBLEM_STATUSES",
    "SourceStatus",
    "default_classify",
    "merge_source_status",
]

logger = structlog.get_logger()

#: Статусы, при которых ответ сборщика — пробел, а не наблюдение.
PROBLEM_STATUSES = frozenset({
    SourceStatus.UNAVAILABLE, SourceStatus.FAILED, SourceStatus.INVALID,
})


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Outcome:
    """Явный исход, который сборщик может вернуть вместо голых данных.

    Нужен там, где статус не выводится из самих данных: снапшот k8s,
    собранный при упавшем API (данные-заглушка есть, наблюдения нет), или
    пустой список деплоев при мёртвом потоке.
    """

    status: SourceStatus
    data: Any = None
    reason: Optional[str] = None
    # Тип исключения, которое клиент поймал сам (снапшот k8s глушит сбой
    # API внутри) — чтобы FAILED не терял его в CollectorResult.error.
    error: Optional[str] = None


@dataclass
class CollectorResult:
    """Результат одного прогона сборщика.

    `reason` — ровно та строка, что уйдёт в `source_status[поле]`. Её читают
    embed и timeline, поэтому формулировки («kg_deployments недоступен:
    OperationalError», «k8s API не ответил») сохранены из прежнего кода.
    `error` — тип исключения, для логов и метрик.
    """

    name: str
    status: SourceStatus
    data: Any = None
    ctx_fields: Tuple[str, ...] = ()
    provenance: str = ""
    started_at: datetime = field(default_factory=_now)
    finished_at: datetime = field(default_factory=_now)
    error: Optional[str] = None
    reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        """True, если ответ — наблюдение (в том числе пустое)."""
        return self.status not in PROBLEM_STATUSES

    def source_status_entries(self) -> Dict[str, str]:
        """Записи Known Unknowns для этого результата: {поле ctx: причина}."""
        if self.ok:
            return {}
        why = self.reason or self.status.value
        return {f: why for f in self.ctx_fields}

    def to_dict(self) -> Dict[str, Any]:
        """Сводка без `data` — для трейсов и аудита (данные бывают большими)."""
        return {
            "name": self.name,
            "status": self.status.value,
            "ctx_fields": list(self.ctx_fields),
            "provenance": self.provenance,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_ms": int(
                (self.finished_at - self.started_at).total_seconds() * 1000
            ),
            "error": self.error,
            "reason": self.reason,
        }


def merge_source_status(
    dst: MutableMapping[str, str],
    result: CollectorResult,
    *,
    overwrite: bool = True,
) -> None:
    """Внести записи результата в `source_status`.

    `overwrite=False` — не перетирать уже поставленную причину: первая
    причина конкретнее («kg_alerts недоступен: …» важнее общего «сервис не
    найден в KG»).
    """
    for f, why in result.source_status_entries().items():
        if overwrite:
            dst[f] = why
        else:
            dst.setdefault(f, why)


def default_classify(data: Any) -> Outcome:
    """None → UNAVAILABLE, пустая коллекция → EMPTY, иначе SUCCESS.

    None — принятое в кодовой базе «не знаю» (см. `fetch_node_namespaces`,
    `fetch_live_replicas`): клиент сам глушит сбой и отдаёт None.
    """
    if data is None:
        return Outcome(SourceStatus.UNAVAILABLE, None)
    if isinstance(data, (list, tuple, dict, set, str)) and not data:
        return Outcome(SourceStatus.EMPTY, data)
    return Outcome(SourceStatus.SUCCESS, data)


@dataclass(frozen=True)
class Collector:
    """Описание сборщика: что наполняет, откуда берёт, как сообщает о сбое.

    `failure_label` — префикс причины при исключении: итоговая строка
    `f"{failure_label}: {ИмяИсключения}"`. `failure_reason` — готовая
    причина при исключении вместо этой склейки (там, где прежний текст не
    называл исключение, а embed его уже рисует). `unavailable_reason` — причина,
    когда сборщик вернул None / UNAVAILABLE без своей формулировки.
    `log_event` — имя structlog-события при исключении; по умолчанию
    `collector.<name>_failed`, но переведённые сборщики сохраняют прежние
    имена, чтобы не ломать поиск в логах.
    """

    name: str
    ctx_fields: Tuple[str, ...] = ()
    provenance: str = ""
    timeout_seconds: Optional[float] = None
    failure_label: Optional[str] = None
    failure_reason: Optional[str] = None
    unavailable_reason: Optional[str] = None
    log_event: Optional[str] = None

    # --- прогон ---------------------------------------------------------

    def run_sync(
        self,
        fn: Callable[..., Any],
        *args: Any,
        classify: Callable[[Any], Outcome] = default_classify,
        **kwargs: Any,
    ) -> CollectorResult:
        """Синхронный прогон: исключение → FAILED, None → UNAVAILABLE."""
        started = _now()
        try:
            data = fn(*args, **kwargs)
        except Exception as e:
            return self._failed(e, started)
        return self._classified(data, started, classify)

    async def run(
        self,
        fn: Callable[..., Awaitable[Any]],
        *args: Any,
        classify: Callable[[Any], Outcome] = default_classify,
        **kwargs: Any,
    ) -> CollectorResult:
        """Асинхронный прогон с дедлайном `timeout_seconds`.

        Таймаут → UNAVAILABLE: до источника не дошли, судить нечего. Это не
        FAILED — исключения по дороге не было, был только ожидающий ответ.
        """
        started = _now()
        try:
            if self.timeout_seconds is not None:
                data = await asyncio.wait_for(
                    fn(*args, **kwargs), timeout=self.timeout_seconds,
                )
            else:
                data = await fn(*args, **kwargs)
        except asyncio.TimeoutError:
            logger.warning(
                self.log_event or f"collector.{self.name}_timeout",
                collector=self.name, timeout_s=self.timeout_seconds,
            )
            label = self.failure_label or self.name
            return self._result(
                SourceStatus.UNAVAILABLE, None, started,
                error="TimeoutError",
                reason=f"{label}: таймаут {self.timeout_seconds:g}с",
            )
        except Exception as e:
            return self._failed(e, started)
        return self._classified(data, started, classify)

    # --- внутреннее -----------------------------------------------------

    def _failed(self, e: Exception, started: datetime) -> CollectorResult:
        logger.warning(
            self.log_event or f"collector.{self.name}_failed",
            collector=self.name, error=str(e),
        )
        label = self.failure_label or self.name
        return self._result(
            SourceStatus.FAILED, None, started,
            error=type(e).__name__,
            reason=self.failure_reason or f"{label}: {type(e).__name__}",
        )

    def _classified(
        self, data: Any, started: datetime, classify: Callable[[Any], Outcome],
    ) -> CollectorResult:
        out = data if isinstance(data, Outcome) else classify(data)
        reason = out.reason
        if reason is None and out.status in PROBLEM_STATUSES:
            reason = self.unavailable_reason
        return self._result(
            out.status, out.data, started, error=out.error, reason=reason,
        )

    def _result(
        self,
        status: SourceStatus,
        data: Any,
        started: datetime,
        *,
        error: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> CollectorResult:
        return CollectorResult(
            name=self.name,
            status=status,
            data=data,
            ctx_fields=self.ctx_fields,
            provenance=self.provenance,
            started_at=started,
            finished_at=_now(),
            error=error,
            reason=reason,
        )
