"""Проваленная или незавершённая миграция схемы.

Самый частый класс поломок сквадов в live-RCA датасете (10 из 31 реального
инцидента, 24.09.2026) — и до этого правила ни один факт на него не
указывал: признаки лежали в логах и событиях, но пайплайн превращал их в
ноль фактов, гипотез не было, модель честно воздерживалась.

Что считаем признаком (подтипы в evidence.signals):

  * `dirty`          — golang-migrate «Dirty database version N» или
                       `schema_migrations … dirty=true`: миграция упала
                       посередине, версия заблокирована до ручного force.
  * `migration_error`— явная ошибка мигратора («migration failed», alembic
                       CommandError / «Can't locate revision»).
  * `migrate_job`    — Job мигратора в BackoffLimitExceeded/DeadlineExceeded
                       (событие) или с failed_count>0 без успеха в графе
                       (`kg_jobs`, kg_k8s_jobs на момент инцидента)
                       или его под не стартовал из-за образа (ImagePullBackOff
                       у `*migrat*`: тег без образа мигратора — отдельный
                       класс image_pull, здесь только кросс-ссылка
                       `related="image_pull"`).
  * `orleans_column` — Orleans-хранилище «Field not found in row: X»: код
                       ждёт колонку, которой в схеме нет — пропущенная
                       миграция, а не баг кода.
  * `schema_mismatch`— postgres «column/relation … does not exist». Сам по
                       себе слабый сигнал (опечатка в запросе даёт то же),
                       поэтому confidence выше только при деплое в окне.

Исходы:
  FOUND  — хотя бы один сигнал; confidence — по самому сильному.
  ABSENT — текст логов/снимка был и просмотрен, сигналов нет. Уверенность
           умеренная: отсутствие фразы в выборке логов — не доказательство.
  (нет факта) — смотреть было не во что (ни логов, ни снимка, ни событий):
           «не проверяли» не должно читаться как ✗.
  UNKNOWN — источник упал: при пустых полях — явный ?, иначе ✗ понижает
           до ? Rule.run() через source_status.

Привязка (как в oom.py / pod_events.py): без pod/service в алерте снимок и
события собираются по всему namespace-у, и упавшая миграция соседнего
сервиса иначе стала бы жёстким якорем чужого инцидента. Такие находки — в
soft-зоне критика; Job мигратора другого workload-а при известном target —
тоже.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.diagnostics.facts import Fact, FactKind
from app.diagnostics.rules.base import Rule, same_workload
from app.diagnostics.rules.pod_events import _event_object

_DIRTY_VERSION = re.compile(r"dirty database version\s*(\d+)", re.IGNORECASE)
_DIRTY_FLAG = re.compile(
    r"schema_migrations[^\n]{0,80}?dirty\s*[=:]\s*(?:true|t)\b", re.IGNORECASE,
)
_MIGRATION_ERROR = re.compile(
    r"(migration\s+failed|failed to run migrations?|"
    r"alembic\.util\.exc\.commanderror|can't locate revision)",
    re.IGNORECASE,
)
_ORLEANS_FIELD = re.compile(r"field not found in row:\s*([\w.]+)", re.IGNORECASE)
_MISSING_SCHEMA_OBJECT = re.compile(
    r"\b(column|relation)\s+\"?([\w.]+)\"?\s+does not exist", re.IGNORECASE,
)

_JOB_FAILED_REASONS = ("backofflimitexceeded", "deadlineexceeded")
_IMAGE_REASONS = ("imagepullbackoff", "errimagepull")
_MIGRATE_TOKEN = "migrat"

# (подтип → confidence). Dirty/ошибка мигратора/упавший Job — прямые
# наблюдения; колонка Orleans — почти прямое; «does not exist» — косвенное.
_CONF = {
    "dirty": 0.95,
    "migrate_job": 0.9,
    "migration_error": 0.85,
    "orleans_column": 0.85,
    "schema_mismatch_after_deploy": 0.75,
    "schema_mismatch": 0.55,
}
# Уверенность ✗: просмотрели выборку логов/снимка, сигналов нет.
_ABSENT_CONFIDENCE = 0.6
# Находка, которую не к чему привязать (алерт без pod/service → материал со
# всего namespace-а) или Job мигратора чужого workload-а. Soft-зона
# fact_critic [0.25, 0.5): гипотеза живёт, но жёстким якорем не становится.
_UNATTRIBUTED_CONFIDENCE = 0.45
_MAX_OBJECTS = 3


def _migrate_job_signal(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Упавший Job мигратора или его под без образа — из k8s_events."""
    for ev in events:
        if not isinstance(ev, dict):
            continue
        obj = _event_object(ev)
        if _MIGRATE_TOKEN not in obj.lower():
            continue
        reason = str(ev.get("reason") or "")
        low = reason.lower()
        message = str(ev.get("message") or "").lower()
        if any(r in low for r in _JOB_FAILED_REASONS) or "backofflimitexceeded" in message:
            return {"job": obj, "reason": reason}
        if any(r in low for r in _IMAGE_REASONS) or "imagepullbackoff" in message:
            # Отдельный класс image_pull ведёт своё правило; здесь фиксируем
            # только, что не стартовал именно мигратор.
            return {"job": obj, "reason": reason, "related": "image_pull"}
    return None


def _kg_migrate_job_signal(jobs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Упавший Job мигратора из графа (kg_k8s_jobs на момент инцидента).

    Живой API к разбору его часто уже не показывает: Job пересоздан следующим
    деплоем или снесён ttl, а граф помнит failed_count. Строка, обновлённая
    уже после инцидента (`state_after_as_of`), наблюдением на его момент не
    считается.
    """
    for j in jobs:
        if not isinstance(j, dict) or not j.get("migrate") or j.get("state_after_as_of"):
            continue
        if (j.get("failed") or 0) > 0 and not (j.get("succeeded") or 0):
            reason = "Failed"
            if j.get("exit_code") is not None:
                reason += f" exit_code={j['exit_code']}"
            return {"job": str(j.get("name") or ""), "reason": reason}
    return None


def _recent_deploy_present(ctx: Dict[str, Any]) -> bool:
    return bool(ctx.get("recent_deployments"))


class MigrationFailedRule(Rule):
    name = "MigrationFailedRule"
    sources = ("k8s_events", "k8s_summary", "logs_summary", "kg_jobs")

    def evaluate(self, ctx: Dict[str, Any]) -> List[Fact]:
        text = self.text_haystack(ctx)
        events = [e for e in (ctx.get("k8s_events") or []) if isinstance(e, dict)]
        target = ctx.get("pod") or ctx.get("service")
        subject = ctx.get("service") or ctx.get("pod") or ctx.get("namespace")

        signals: List[str] = []
        evidence: Dict[str, Any] = {}

        versions = _DIRTY_VERSION.findall(text)
        if versions or _DIRTY_FLAG.search(text):
            signals.append("dirty")
            evidence["dirty"] = True
            if versions:
                evidence["version"] = versions[0]

        job = _migrate_job_signal(events) or _kg_migrate_job_signal(ctx.get("kg_jobs") or [])
        if job:
            signals.append("migrate_job")
            evidence["job"] = job["job"]
            evidence["job_reason"] = job["reason"]
            if job.get("related"):
                evidence["related"] = job["related"]

        err = _MIGRATION_ERROR.search(text)
        if err:
            signals.append("migration_error")
            evidence["error"] = err.group(1)[:60]

        fields = _ORLEANS_FIELD.findall(text)
        if fields:
            signals.append("orleans_column")
            evidence["missing_fields"] = sorted(set(fields))[:_MAX_OBJECTS]

        missing = _MISSING_SCHEMA_OBJECT.findall(text)
        if missing:
            after_deploy = _recent_deploy_present(ctx)
            signals.append("schema_mismatch_after_deploy" if after_deploy else "schema_mismatch")
            evidence["missing_objects"] = sorted({f"{k.lower()} {n}" for k, n in missing})[:_MAX_OBJECTS]
            evidence["after_recent_deploy"] = after_deploy

        if signals:
            confidence = max(_CONF[s] for s in signals)
            attribution = self._attribution(target, evidence.get("job"), signals)
            if attribution != "scoped":
                confidence = min(confidence, _UNATTRIBUTED_CONFIDENCE)
                evidence["attribution"] = attribution
            return [Fact(
                kind=FactKind.MIGRATION_FAILED,
                observed=True,
                confidence=confidence,
                subject=subject,
                evidence={"signals": signals, **evidence},
                source_rule=self.name,
            )]

        if not self._scanned_anything(ctx, events):
            # Смотреть было не во что. Если это потому, что источник упал, —
            # явный ?: без факта критик не отличит «не проверяли» от «нет».
            failed = self.failed_sources(ctx)
            if failed:
                return [Fact.unknown(
                    FactKind.MIGRATION_FAILED,
                    "; ".join(f"{src}: {why}" for src, why in failed.items()),
                    subject=subject, source_rule=self.name,
                )]
            return []
        return [Fact(
            kind=FactKind.MIGRATION_FAILED,
            observed=False,
            confidence=_ABSENT_CONFIDENCE,
            subject=subject,
            evidence={"note": "no migration signals in scanned logs/snapshot/events"},
            source_rule=self.name,
        )]

    @staticmethod
    def _attribution(target: Optional[str], job: Optional[str], signals: List[str]) -> str:
        """scoped | unverified (нет target) | foreign (Job чужого workload-а).

        Текстовые сигналы при известном target приходят из уже скоупленного
        снимка/логов (k8s_facts), поэтому считаются привязанными. Job
        мигратора сверяется по имени: `bravo-migrate` ↔ `bravo-service`.
        """
        if not target:
            return "unverified"
        if job and signals == ["migrate_job"] and not same_workload(job, target):
            return "foreign"
        return "scoped"

    @staticmethod
    def _scanned_anything(ctx: Dict[str, Any], events: List[Dict[str, Any]]) -> bool:
        """Был ли вообще наблюдаемый материал, кроме самого алерта."""
        return bool(ctx.get("logs_summary") or ctx.get("k8s_summary") or events
                    or ctx.get("kg_jobs"))

