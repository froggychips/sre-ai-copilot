"""Наблюдения squad-medic: что медик ВИДЕЛ на стенде, без его выводов.

Медик ходит в кластер вживую (kubectl, psql) и видит то, чего граф не
сохраняет: `schema_migrations` с dirty/phantom-версией, отсутствующие ключи
Secret, коды выхода контейнеров. Это наблюдения — их место в графе рядом с
kg_pod_events и kg_k8s_jobs, как ещё один источник контекста инцидента.
Но медик — не замена графу и не арбитр причины: его root_cause/next_action
— вывод (часто догадка модели медика) и сюда не попадает никогда.

Экстрактор живёт здесь, а не в скрипте датасета: наблюдения извлекаются
ОДИН раз при приёме события (`POST /webhooks/remediation` →
`kg_remediation_events.observations`), а сборщик контекста из графа
(`app/context/kg_incident_context.py`) читает их как источник `squad-medic`.
Для событий, принятых до этого поля, тот же экстрактор применяется к
сохранённым полям события — одна логика, а не две.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

#: Версия схемы структурированных наблюдений в kg_remediation_events.observations.
OBSERVATIONS_SCHEMA = "medic_obs/v1"
#: Провенанс источника — так он подписан в k8s_summary и source_status.
PROVENANCE = "squad-medic"

# Снимка кластера на момент инцидента в БД нет, а медик его видел: в summary,
# applied, manual и gaps события лежит то, что он наблюдал (состояния подов,
# коды выхода, dirty-миграция, отсутствующие ключи секрета). Но там же лежат
# и его ВЫВОДЫ («теги ушли из реестра из-за retention») — а вывод и есть
# ответ, с которым сверяется модель. Поэтому текст медика в вход модели не
# копируется НИКОГДА: экстрактор ищет белый список наблюдаемых сигналов и
# рендерит каждый своим шаблоном. Из исходного текста в вход попадают только
# захваченные токены — reason из словаря k8s, числа, имена ключей и таблиц.
# root_cause и next_action экстрактор не читает вовсе.

_POD_REASONS = (
    "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError",
    "CreateContainerError", "RunContainerError", "CrashLoopBackOff",
    "OOMKilled", "Evicted", "ContainerCreating", "Init:CrashLoopBackOff",
    "Init:Error", "FailedScheduling", "FailedMount", "Pending",
)
_REASON_RE = re.compile(
    r"(?<![\w:])(" + "|".join(re.escape(r) for r in _POD_REASONS) + r")(?![\w])", re.I
)
_EXIT_RE = re.compile(r"(?:exit(?:\s*code)?|exitcode|код(?:ом)?\s+выхода)\s*[=:]?\s*(\d{1,3})\b", re.I)
_SIGNAL_RE = re.compile(r"\bSIG(SEGV|ABRT|KILL|TERM|BUS|ILL|FPE)\b")
_RESTARTS_RE = re.compile(r"(\d{1,6})\s*(?:рестарт\w*|restarts?)\b|restarts?\s*[=:]\s*(\d{1,6})", re.I)
_FAILED_PODS_RE = re.compile(r"(?:(\d{1,4})\s*)?(failed|evicted)[\s-]*(?:под\w*|pods?)\b", re.I)
_DIRTY_RE = re.compile(r"\bdirty\b", re.I)
_MIGRATION_VERSION_RE = re.compile(r"(?:верси\w+|version|v)\s*[=:]?\s*(\d{8,14})\b", re.I)
_RELATION_RE = re.compile(r'(relation|column)\s+"?([\w.]{1,80})"?\s+does not exist', re.I)
_PERMISSION_RE = re.compile(r"permission denied for (table|schema|relation|database|sequence)\s+\"?([\w.]{1,80})", re.I)
_SECRET_CTX_RE = re.compile(r"secret|секрет", re.I)
_MISSING_RE = re.compile(r"нет|отсутств|не хвата|недолит|missing|couldn't find|not found", re.I)
_ENV_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]*_[A-Z0-9_]*\*?|[A-Z]{3,}_\*)")
_COULDNT_FIND_KEY_RE = re.compile(r"couldn't find key\s+([A-Za-z0-9_.-]{1,80})", re.I)
_ORLEANS_RE = re.compile(r"orleans|membership|силос|silo", re.I)
_ZOMBIE_RE = re.compile(r"zombie|status\s*=\s*6|мёртв\w* (?:силос|запис)|dead silo", re.I)
_PROBE_RE = re.compile(r"\b(readiness|liveness|startup)[\s-]*probe\b", re.I)
_CONN_REFUSED_RE = re.compile(r"connection refused", re.I)

# Каналы k8s-событий, которые понимает PodEventsRule: так наблюдение станет
# фактом FactStore (OOM, evicted, crashloop, scheduling), а не только строкой.
_REASON_TO_EVENT = {
    "crashloopbackoff": "BackOff", "init:crashloopbackoff": "BackOff",
    "oomkilled": "OOMKilled", "evicted": "Evicted",
    "failedscheduling": "FailedScheduling",
}


def _medic_texts(event: Dict[str, Any]) -> List[str]:
    """Тексты медика, из которых МОЖНО извлекать наблюдения.

    root_cause и next_action сюда не входят намеренно: это вывод и план,
    то есть ответ. summary/applied/manual/gaps тоже содержат выводы, но из
    них берутся только совпадения белого списка, а не фразы.
    """
    out: List[str] = []
    if event.get("summary"):
        out.append(str(event["summary"]))
    for key in ("applied", "manual", "gaps"):
        val = event.get(key)
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except ValueError:
                val = [val]
        if isinstance(val, list):
            out.extend(str(x) for x in val if x)
    extras = event.get("extras")
    if isinstance(extras, dict):
        out.extend(str(v) for v in extras.values() if isinstance(v, (str, int, float)))
    return out


def _sentences(text: str) -> List[str]:
    return [s for s in re.split(r"(?<=[.;!?])\s+|\n+", text) if s.strip()]


def extract_medic_observations(event: Dict[str, Any]) -> List[str]:
    """Наблюдаемые факты из события медика — строки фиксированных шаблонов."""
    texts = _medic_texts(event)
    reasons: Dict[str, str] = {}
    exits: Set[int] = set()
    signals: Set[str] = set()
    restarts = 0
    failed_pods = False
    dirty_versions: Set[str] = set()
    dirty = False
    db_errors: Set[str] = set()
    secret_keys: Set[str] = set()
    orleans_zombie = False
    probes: Set[str] = set()
    conn_refused = False
    for text in texts:
        for m in _REASON_RE.finditer(text):
            canon = next(r for r in _POD_REASONS if r.lower() == m.group(1).lower())
            reasons[canon.lower()] = canon
        exits |= {int(x) for x in _EXIT_RE.findall(text) if int(x) <= 255}
        signals |= {f"SIG{s}" for s in _SIGNAL_RE.findall(text)}
        for a, b in _RESTARTS_RE.findall(text):
            restarts = max(restarts, int(a or b or 0))
        failed_pods = failed_pods or bool(_FAILED_PODS_RE.search(text))
        for m in _RELATION_RE.finditer(text):
            db_errors.add(f'{m.group(1).lower()} "{m.group(2)}" does not exist')
        for m in _PERMISSION_RE.finditer(text):
            db_errors.add(f'permission denied for {m.group(1).lower()} "{m.group(2)}"')
        secret_keys |= set(_COULDNT_FIND_KEY_RE.findall(text))
        for sent in _sentences(text):
            if _DIRTY_RE.search(sent):
                dirty = True
                dirty_versions |= set(_MIGRATION_VERSION_RE.findall(sent))
            if _SECRET_CTX_RE.search(sent) and _MISSING_RE.search(sent):
                secret_keys |= set(_ENV_KEY_RE.findall(sent))
            if _ORLEANS_RE.search(sent) and _ZOMBIE_RE.search(sent):
                orleans_zombie = True
        probes |= {p.lower() for p in _PROBE_RE.findall(text)}
        conn_refused = conn_refused or bool(_CONN_REFUSED_RE.search(text))

    facts: List[str] = []
    if reasons:
        facts.append("состояние подов: " + ", ".join(sorted(reasons.values())))
    if exits:
        facts.append("код выхода контейнера: " + ", ".join(str(x) for x in sorted(exits)))
    if signals:
        facts.append("сигнал завершения процесса: " + ", ".join(sorted(signals)))
    if restarts:
        facts.append(f"рестартов контейнера: до {restarts}")
    if failed_pods:
        facts.append("в namespace есть поды в фазе Failed/Evicted")
    if dirty:
        v = f" (версия {', '.join(sorted(dirty_versions))})" if dirty_versions else ""
        facts.append(f"schema_migrations: dirty=true{v}")
    for err in sorted(db_errors):
        facts.append(f"ошибка БД: {err}")
    if secret_keys:
        facts.append("в Secret нет ключей: " + ", ".join(sorted(secret_keys)))
    if orleans_zombie:
        facts.append("Orleans membership: есть записи мёртвых силосов (status=6)")
    if probes:
        facts.append("проба не проходит: " + ", ".join(sorted(probes)))
    if conn_refused:
        facts.append("в логах: connection refused")
    return facts


def observation_events(facts: List[str]) -> List[Dict[str, Any]]:
    """k8s-события для PodEventsRule из строки «состояние подов: …»."""
    events: List[Dict[str, Any]] = []
    for f in facts:
        if not f.startswith("состояние подов: "):
            continue
        for r in f.split(": ", 1)[1].split(", "):
            reason = _REASON_TO_EVENT.get(r.lower())
            if reason:
                events.append({"type": "Warning", "reason": reason,
                               "message": f"observed by squad-medic: {r}", "count": 1})
    return events


def answer_leak(facts: List[str], root_cause: Optional[str], n: int = 4) -> List[str]:
    """n-граммы слов root_cause, которые нашлись в фактах (пусто = утечки нет).

    Страховка поверх белого списка: шаблоны сами фраз медика не содержат,
    но если когда-нибудь захваченный токен окажется куском вывода — кейс
    не должен тихо уехать в датасет с подсказкой.
    """
    def words(s: str) -> List[str]:
        return re.findall(r"[\w*]+", s.lower())

    rc = words(root_cause or "")
    grams = {" ".join(rc[i:i + n]) for i in range(len(rc) - n + 1)}
    text = " ".join(" ".join(words(f)) for f in facts)
    return sorted(g for g in grams if g and g in text)


def build_observations(event: Dict[str, Any], observed_at: Optional[datetime] = None) -> Dict[str, Any]:
    """Структурированная запись наблюдений события медика для графа.

    `observed_at` — начало прогона медика: наблюдения сняты с живого стенда
    в этот момент. `namespace`/`namespaces` — стенд, на котором он смотрел.
    Выводы события (root_cause/next_action) не читаются.
    """
    at = observed_at or event.get("started_at")
    if isinstance(at, datetime):
        at_s: Optional[str] = at.isoformat()
    else:
        at_s = str(at) if at else None
    return {
        "schema": OBSERVATIONS_SCHEMA,
        "provenance": PROVENANCE,
        "observed_at": at_s,
        "namespace": event.get("namespace"),
        "namespaces": list(event.get("namespaces") or []),
        "facts": extract_medic_observations(event),
    }


def observations_of(row: Dict[str, Any]) -> Dict[str, Any]:
    """Наблюдения события: сохранённые при приёме или, для событий до поля
    `observations`, — тем же экстрактором из сохранённых полей."""
    stored = row.get("observations")
    if isinstance(stored, str):
        try:
            stored = json.loads(stored)
        except ValueError:
            stored = None
    if isinstance(stored, dict) and stored.get("schema") == OBSERVATIONS_SCHEMA:
        return stored
    return build_observations(row)
