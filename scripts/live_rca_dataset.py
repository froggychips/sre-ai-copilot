#!/usr/bin/env python3
"""Live RCA-датасет: реальные инциденты с известной причиной → живая модель.

Golden-набор меряет разбор на синтетике, которую мы же и написали. Этот
скрипт берёт инциденты, у которых причину установил не copilot, а squad-medic
(`kg_remediation_events.root_cause`, `fixed=true`), и прогоняет через ту же
LLM-цепочку, что и golden (`MultiHypothesisAgent` → `FactCriticAgent` →
`best_candidate`), без Discord, без executor-а и без записи в БД.

Ключ не нужен: по умолчанию `LLM_BACKEND=claude_cli` — локальный
залогиненный `claude --print`.

Подкоманды:
  export  SELECT из боевой БД copilot (через `kubectl exec psql`, транзакция
          read-only) → cases.jsonl. Только чтение.
  kg-context  дописать в готовый cases.jsonl снимок контекста и реконструкцию
          из графа на момент инцидента (`context=kg_reconstructed`). Только чтение.
  run     прогон N кейсов через модель → results.jsonl (дописывает, уже
          прогнанные пропускает — прогон можно дробить).
  score   метрики по results.jsonl → summary.json + таблица.

ДАННЫЕ — ТОЛЬКО ВНЕ РЕПО. Репозиторий публичный, а в кейсах живые namespace-ы,
имена сервисов, текст алертов и выводы медика. По умолчанию всё пишется в
`~/.cache/sre-ai-copilot/live-rca/<дата>/`; путь внутри репо скрипт отвергает.

Оценка — грубая и честная: причину медика и причину модели раскладываем по
одним и тем же классам (`CAUSE_CLASSES`, регулярки RU+EN), попадание — это
пересечение классов. Это нижняя оценка: модель могла назвать ту же причину
словами, которых нет в регулярках. У кейса несколько источников входа
(`context` — метка режима, `had_snapshot`/`kg_context` — что подмешано):

  alert_only      только алерт;
  medic_observed  алерт + наблюдения медика (`extract_medic_observations`):
                  состояния подов, коды выхода, dirty-миграция, отсутствующие
                  ключи секрета — шаблонами из белого списка, без его выводов.

Поверх режима `run` накладывает снимок контекста инцидента
(`analysis.context_snapshot`, если пайплайн его записал) и реконструкцию из
графа (`kg-context`). Метрики считаются по режимам раздельно. medic_observed —
нижняя граница «разбора с контекстом», а не он сам: медик записал не всё, что
видел.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_OUT = Path.home() / ".cache" / "sre-ai-copilot" / "live-rca" / date.today().isoformat()

# Классы причин. Порядок не важен: и медик, и модель могут попасть в
# несколько классов, попадание = непустое пересечение.
CAUSE_CLASSES: Dict[str, str] = {
    "image_missing": r"imagepull|errimagepull|retention|реестр|registry|образ\w* .{0,40}(нет|отсутств|не зал|снес|удал)|тег\w* .{0,30}(нет|снес|удал|отсутств)|image .{0,30}(missing|not found|absent)|tag .{0,30}(missing|deleted|not found)",
    "migration": r"миграц|migrat|dirty|relation .{0,30}(does not exist|не существ)|schema|схем",
    "crash_after_deploy": r"crashloop|startup probe|readiness|connection refused|не слушает|не поднимает порт|падает на старте|fails? on start",
    "stale_failed_pods": r"failed-под|failed pod|stale|evicted|ephemeral|остат",
    "orleans_membership": r"orleans|membership|силос|silo",
    "secret_config": r"secret|секрет|ключ\w* секрета|env|config(map)?|конфиг",
    "db_grants": r"grant|привилег|permission denied|прав\w* .{0,20}(бд|db)",
    "resources": r"oom|memory|памят|cpu|throttl|node .{0,20}(pressure|исчерп)|disk|диск",
}
_CLASS_RE = {k: re.compile(v, re.I) for k, v in CAUSE_CLASSES.items()}


def classify(text: Optional[str]) -> Set[str]:
    if not text:
        return set()
    return {k for k, rx in _CLASS_RE.items() if rx.search(text)}


def primary_class(text: Optional[str]) -> Optional[str]:
    """Класс, упомянутый в тексте раньше остальных.

    Медик пишет причину первой фразой, а следствия и попутные находки —
    дальше («образов нет в реестре …, миграции этих сервисов …»). Мультиметка
    по всему тексту делала попадание слишком лёгким: «migration» ловилась
    почти у половины кейсов как побочное упоминание.
    """
    if not text:
        return None
    hits = [(m.start(), k) for k, rx in _CLASS_RE.items() if (m := rx.search(text))]
    return min(hits)[1] if hits else None


# --- наблюдения медика ----------------------------------------------------
#
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


def _guard_out_dir(out: Path) -> Path:
    out = out.expanduser().resolve()
    if out == REPO_ROOT or REPO_ROOT in out.parents:
        raise SystemExit(f"датасет внутри репо запрещён (репо публичный): {out}")
    out.mkdir(parents=True, exist_ok=True)
    return out


# --- export ---------------------------------------------------------------

_EXPORT_SQL = """
SET TRANSACTION READ ONLY;
SELECT row_to_json(t) FROM (
  SELECT e.id AS event_id, e.started_at, e.outcome, e.root_cause, e.summary,
         e.applied, e.manual, e.gaps, e.extras,
         i.incident_key, i.namespace, i.service_name, i.severity,
         i.alertnames, i.opened_at,
         al.alertname AS alert_name, al.description AS alert_description
  FROM kg_remediation_events e
  JOIN kg_incidents i ON i.id = e.incident_id
  -- Алерт, который горел К МОМЕНТУ разбора медика, и его имя вместе с его же
  -- описанием: самый свежий алерт инцидента мог прийти после разбора, а имя
  -- из incident-wide alertnames — от другого алерта той же группы.
  LEFT JOIN LATERAL (
    SELECT a.alertname, a.raw->>'description' AS description FROM kg_alerts a
    WHERE a.incident_id = i.incident_key AND a.fired_at <= e.started_at
    ORDER BY a.fired_at DESC LIMIT 1
  ) al ON true
  WHERE e.fixed AND e.actor = 'squad-medic' AND length(coalesce(e.root_cause, '')) > 40
    AND e.started_at > now() - make_interval(days => {days})
  ORDER BY e.started_at DESC
) t;
"""


# Снимок контекста инцидента (analysis.context_snapshot, с 1.0.20 —
# app/context/incident_snapshot.py): связь событие медика → kg_incidents →
# записи incidents по fingerprint-ам. Берётся последний снимок, записанный ДО
# разбора медика: снимок после починки описывал бы уже здоровый стенд.
_SNAPSHOT_SQL = """
SET TRANSACTION READ ONLY;
SELECT to_jsonb(t) FROM (
  SELECT DISTINCT ON (e.id) e.id AS event_id,
         to_jsonb(r.analysis) -> 'context_snapshot' AS context_snapshot
  FROM kg_remediation_events e
  JOIN kg_incidents i ON i.id = e.incident_id
  JOIN incidents r ON r.incident_id IN (
    SELECT jsonb_array_elements_text(coalesce(to_jsonb(i.fingerprints), '[]'::jsonb)))
  -- Отсечка по времени САМОГО снимка: строка инцидента переиспользуется
  -- при повторном срабатывании, и снимок после починки перезаписал бы
  -- прежний при старом created_at.
  WHERE e.id IN ({ids})
    AND jsonb_typeof(to_jsonb(r.analysis) -> 'context_snapshot') = 'object'
    AND (to_jsonb(r.analysis) #>> '{{context_snapshot,captured_at}}')::timestamptz <= e.started_at
  ORDER BY e.id, (to_jsonb(r.analysis) #>> '{{context_snapshot,captured_at}}') DESC
) t;
"""


def _psql_rows(args, sql: str) -> Optional[List[Dict[str, Any]]]:
    """SELECT через `kubectl exec psql` в read-only транзакции → строки JSON."""
    cmd = [
        "kubectl", "--context", args.context, "-n", args.namespace, "exec", "postgres-0",
        "--", "psql", "-U", args.db_user, "-d", args.db_name, "-At", "-v", "ON_ERROR_STOP=1",
        "-c", "BEGIN;" + sql + "COMMIT;",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        return None
    return [json.loads(ln) for ln in proc.stdout.splitlines() if ln.startswith("{")]


def attach_context_snapshots(args, cases: List[Dict[str, Any]]) -> int:
    """Дописать в кейсы `context_snapshot`, где он есть. Возвращает число
    кейсов со снимком. Сбой запроса не роняет выгрузку: кейс без снимка —
    прежний «разбор по алерту», а не ошибка."""
    ids = [int(c["event_id"]) for c in cases if c.get("event_id") is not None]
    if not ids:
        return 0
    rows = _psql_rows(args, _SNAPSHOT_SQL.format(ids=",".join(map(str, ids))))
    by_id = {r["event_id"]: r.get("context_snapshot") for r in rows or []}
    n = 0
    for c in cases:
        snap = by_id.get(c.get("event_id"))
        if isinstance(snap, dict):
            c["context_snapshot"] = snap
            n += 1
    return n


def ctx_from_snapshot(ctx: Dict[str, Any], snap: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Наложить сохранённый снимок на ctx правил: правила и модель видят то,
    что видел пайплайн в момент инцидента, а не один алерт."""
    if not isinstance(snap, dict) or snap.get("schema") != "incident_ctx/v1":
        return ctx
    # События/деплои/алерты в снимке — ссылки (kg_refs), их данные поднимает
    # reconstruct_from_kg; здесь — только то, чего в графе нет.
    for key in ("k8s_pod_state", "metrics_summary", "cluster_health",
                "rollout_suppressed", "core_dump_node", "logs_summary"):
        if snap.get(key) is not None:
            ctx[key] = snap[key]
    # Пробелы источников переносятся вместе с данными: иначе правило по
    # непришедшему источнику снова скажет уверенное «не было».
    status = dict(ctx.get("source_status") or {})
    status.update(snap.get("source_status") or {})
    for key in snap.get("truncated") or []:
        field = str(key).split(":", 1)[0]
        if snap.get(field) is None and field != "facts":
            status.setdefault(field, "truncated: срезано лимитом снимка")
    ctx["source_status"] = status
    return ctx


# Реконструкция контекста из графа point-in-time: у большинства старых
# инцидентов снимка нет, но граф хранит историю событий подов, алертов и
# деплоев. Замер 24.09.2026: у 92% событий медика fixed=true есть
# kg_pod_events того же namespace в окне, деплои через kg_services — у 7%.
# Верхняя граница окна — НАЧАЛО разбора медика, а не «+30 минут»: события
# после него описывают уже его починку, и в кейсе протекал бы ответ.
_KG_SQL = """
SET TRANSACTION READ ONLY;
SELECT to_jsonb(t) FROM (
  SELECT e.id AS event_id, sc.scope AS ns_scope, sc.ns AS ns_list,
    (SELECT coalesce(jsonb_agg(x), '[]'::jsonb) FROM (
       -- Последнее событие на (под, reason): иначе 40 мест съедают сотни
       -- одинаковых Unhealthy от readiness-пробы, а BackOff/OOMKilled не влезают.
       -- kg_pod_events — изменяемый агрегат: count и last_seen дописываются
       -- следующими синками. last_seen срезаем по отсечке, а count, если
       -- строка обновлялась ПОСЛЕ неё, неизвестен на момент инцидента → NULL.
       SELECT * FROM (
         SELECT DISTINCT ON (pe.namespace, pe.pod_name, pe.reason)
                pe.namespace, pe.pod_name AS pod, pe.type, pe.reason,
                left(pe.message, 300) AS message,
                CASE WHEN coalesce(pe.last_seen, pe.first_seen) <= e.started_at
                     THEN pe.count END AS count,
                pe.first_seen,
                least(coalesce(pe.last_seen, pe.first_seen), e.started_at) AS last_seen
         FROM kg_pod_events pe
         -- first_seen не старше 7 суток: без нижней границы индекс по
         -- first_seen бесполезен и запрос сканирует всю историю с апреля.
         -- Строка, начавшаяся раньше и всё ещё обновлявшаяся, теряется —
         -- цена приемлемая для датасета.
         WHERE pe.namespace = ANY(sc.ns)
           AND pe.first_seen BETWEEN e.started_at - interval '7 days' AND e.started_at
           AND least(coalesce(pe.last_seen, pe.first_seen), e.started_at)
               >= e.started_at - interval '2 hours'
         ORDER BY pe.namespace, pe.pod_name, pe.reason,
                  least(coalesce(pe.last_seen, pe.first_seen), e.started_at) DESC) d
       ORDER BY (d.namespace = i.namespace) DESC, (d.type = 'Warning') DESC, d.last_seen DESC
       LIMIT 40) x) AS pod_events,
    (SELECT coalesce(jsonb_agg(x), '[]'::jsonb) FROM (
       SELECT s.namespace, s.name AS service, a.alertname, a.severity, a.fired_at,
              CASE WHEN a.resolved_at <= e.started_at THEN a.resolved_at END AS resolved_at
       FROM kg_alerts a JOIN kg_services s ON s.id = a.service_id
       WHERE s.namespace = ANY(sc.ns)
         AND a.fired_at BETWEEN e.started_at - interval '2 hours' AND e.started_at
       ORDER BY a.fired_at DESC LIMIT 20) x) AS alerts,
    (SELECT coalesce(jsonb_agg(x), '[]'::jsonb) FROM (
       -- Статика отдельно от кода: StaticsNewCluster раскатывается веером на
       -- все сервисы сквада (сотни «деплоев» на сервис в месяц), и «недавний
       -- деплой» по ней почти всегда true, хотя кода никто не менял.
       SELECT s.namespace, s.name AS service, d.status, d.buildtype_id, d.started_at,
              CASE WHEN d.finished_at <= e.started_at THEN d.finished_at END AS finished_at,
              'code' AS kind
       FROM kg_deployments d JOIN kg_services s ON s.id = d.service_id
       WHERE s.namespace = ANY(sc.ns)
         AND d.started_at BETWEEN e.started_at - interval '6 hours' AND e.started_at
         AND d.buildtype_id NOT LIKE '%StaticsNewCluster%'
       ORDER BY d.started_at DESC
       LIMIT 10) x) AS deployments,
    -- Статика — только счётчиком: её строки заняли бы весь лимит.
    (SELECT count(*) FROM kg_deployments d JOIN kg_services s ON s.id = d.service_id
     WHERE s.namespace = ANY(sc.ns) AND d.buildtype_id LIKE '%StaticsNewCluster%'
       AND d.started_at BETWEEN e.started_at - interval '6 hours' AND e.started_at
    ) AS statics_rollouts
  FROM kg_remediation_events e
  JOIN kg_incidents i ON i.id = e.incident_id
  -- Сквад живёт в нескольких ns: медик пишет основной `squad-N-shared`, а
  -- деплои и события сервисов ложатся в `squad-N-kingdomX`. Точный джойн по
  -- namespace находил деплои у 7% кейсов, по префиксу сквада — у 76%.
  -- Вне сквадов — точный namespace (в имени ns нет `_`/`%`, LIKE безопасен).
  -- Список ns сквада — из kg_services (таблица маленькая), дальше точное
  -- равенство по списку: LIKE с вычисляемым шаблоном по kg_pod_events
  -- индекс не берёт и упирался в таймаут.
  CROSS JOIN LATERAL (
    SELECT p.scope, array_append(coalesce(
             (SELECT array_agg(DISTINCT s2.namespace) FROM kg_services s2
              WHERE s2.namespace LIKE p.scope), '{{}}'::text[]), i.namespace) AS ns
    FROM (SELECT coalesce(substring(i.namespace from '^(squad-[^-]+-)') || '%',
                          i.namespace) AS scope) p
  ) sc
  WHERE e.id IN ({ids})
) t;
"""


def reconstruct_from_kg(args, cases: List[Dict[str, Any]]) -> int:
    """Дописать в кейсы `kg_context` — строки графа на момент инцидента,
    с меткой `context=kg_reconstructed`. Текст событий проходит redact_pii:
    датасет лежит вне репо, но выводы модели по нему могут попасть в отчёт."""
    from app.services.pii_redaction import redact_pii

    ids = [int(c["event_id"]) for c in cases if c.get("event_id") is not None]
    if not ids:
        return 0
    rows = _psql_rows(args, _KG_SQL.format(ids=",".join(map(str, ids)))) or []
    by_id = {r["event_id"]: r for r in rows}
    n = 0
    for c in cases:
        r = by_id.get(c.get("event_id"))
        if not r or not (r.get("pod_events") or r.get("alerts") or r.get("deployments")
                         or r.get("statics_rollouts")):
            continue
        for ev in r.get("pod_events") or []:
            if ev.get("message"):
                ev["message"] = redact_pii(ev["message"], max_len=300)
        c["kg_context"] = {k: r.get(k) or [] for k in ("pod_events", "alerts", "deployments")}
        c["kg_context"]["ns_scope"] = r.get("ns_scope")
        c["kg_context"]["statics_rollouts"] = int(r.get("statics_rollouts") or 0)
        c.setdefault("context", "kg_reconstructed")
        n += 1
    return n


def ctx_from_kg(ctx: Dict[str, Any], kg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Наложить реконструкцию из графа на ctx правил в форме, которую правила
    читают: k8s_events как у K8sFacts, recent_deployments как у TC-экстрактора
    (скоуп namespace — деплой любого сервиса ns, не обязательно этого)."""
    if not isinstance(kg, dict):
        return ctx
    if kg.get("pod_events"):
        ctx["k8s_events"] = [
            {"type": e.get("type"), "reason": e.get("reason"), "message": e.get("message"),
             "count": e.get("count"), "pod": e.get("pod"), "namespace": e.get("namespace"),
             "last_timestamp": e.get("last_seen") or e.get("first_seen")}
            for e in kg["pod_events"]
        ]
    # В правило recent_deploy — только деплои кода (SQL статику в строки не
    # берёт): веерная раскатка статики сделала бы «недавний деплой» истиной
    # почти для любого кейса сквада. Статика — счётчиком в описание.
    code = kg.get("deployments") or []
    statics = int(kg.get("statics_rollouts") or 0)
    if code:
        ctx["recent_deployments"] = [
            {"name": d.get("service") or d.get("buildtype_id") or "deploy",
             "ts": d.get("finished_at") or d.get("started_at"), "status": d.get("status"),
             "buildtype_id": d.get("buildtype_id"), "namespace": d.get("namespace"),
             "attribution_scope": "namespace"}
            for d in code
        ]
    if statics:
        ctx["description"] = ((ctx.get("description") or "")
                              + f"\nРаскатки статики на сквад за 6ч: {statics}").strip()
    # Соседние алерты графа — не upstream по рёбрам зависимостей (правило
    # UpstreamDegraded ждёт edge_kind), поэтому идут в описание, не в правило.
    if kg.get("alerts"):
        names = sorted({a.get("alertname") for a in kg["alerts"] if a.get("alertname")})
        ctx["description"] = ((ctx.get("description") or "")
                              + f"\nАлерты сквада за 2ч: {', '.join(names)}").strip()
    return ctx


def cmd_kg_context(args) -> int:
    """Ретроактивно: дописать снимки и реконструкцию из графа в УЖЕ собранный
    cases.jsonl, не пересобирая выборку (прогнанные results остаются валидны
    по event_id; перепрогон кейса с новым входом — `run --ids`)."""
    out = _guard_out_dir(Path(args.out))
    path = out / "cases.jsonl"
    cases = [json.loads(ln) for ln in path.read_text().splitlines() if ln]
    with_snapshot = attach_context_snapshots(args, cases)
    with_kg = reconstruct_from_kg(args, cases)
    with path.open("w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False, default=str) + "\n")
    print(f"кейсов: {len(cases)}, со снимком: {with_snapshot}, "
          f"с реконструкцией из графа: {with_kg} → {path}")
    return 0


def cmd_export(args) -> int:
    out = _guard_out_dir(Path(args.out))
    sql = "BEGIN;" + _EXPORT_SQL.format(days=int(args.days)) + "COMMIT;"
    cmd = [
        "kubectl", "--context", args.context, "-n", args.namespace, "exec", "postgres-0",
        "--", "psql", "-U", args.db_user, "-d", args.db_name, "-At", "-v", "ON_ERROR_STOP=1",
        "-c", sql,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        return 1
    # Строка на кейс: `json_agg` psql переносит между элементами, и массив
    # не собирается построчным разбором.
    rows: List[Dict[str, Any]] = [
        json.loads(ln) for ln in proc.stdout.splitlines() if ln.startswith("{")
    ]

    path = out / "cases.jsonl"
    keep: Optional[Set[int]] = None
    if args.keep_ids:
        # Тот же набор кейсов, что уже прогонялся: иначе сравнение «только
        # алерт» против «наблюдения медика» шло бы на разных инцидентах.
        if not path.exists():
            raise SystemExit(f"--keep-ids: нет {path}")
        keep = {json.loads(ln)["event_id"] for ln in path.read_text().splitlines() if ln}

    # Медик часто разбирает один и тот же стенд несколько раз подряд с той же
    # причиной: 114 событий KubeContainerWaiting — почти все «retention снёс
    # теги». Без дедупа датасет меряет один случай сотню раз.
    seen: Set[tuple] = set()
    cases = []
    leaks = 0
    for r in rows:
        if keep is not None and r["event_id"] not in keep:
            continue
        alertnames = r.get("alertnames") or []
        labels = sorted(classify(r.get("root_cause")))
        primary = primary_class(r.get("root_cause"))
        key = (tuple(alertnames), primary, r.get("namespace"))
        if keep is None and key in seen:
            continue
        seen.add(key)
        r["expected_classes"] = labels
        r["expected_primary"] = primary
        observed = extract_medic_observations(r)
        # Строка факта, у которой есть общая n-грамма с выводом медика,
        # выбрасывается, даже если это наблюдение: медик цитирует текст
        # ошибки («relation X does not exist») и в root_cause, и граница
        # «наблюдение / подсказка» тут не проверяема. Остальные факты кейса
        # остаются.
        clean = [f for f in observed if not answer_leak([f], r.get("root_cause"))]
        leaks += len(observed) - len(clean)
        observed = clean
        r["observed_medic"] = observed
        r["context"] = "medic_observed" if observed else "alert_only"
        # Сырые поля медика дальше не нужны: наблюдения уже извлечены, а
        # выводы в applied/manual/gaps — лишний шанс протечь в вход.
        for k in ("applied", "manual", "gaps", "extras"):
            r.pop(k, None)
        cases.append(r)
    if keep is not None:
        missing = keep - {c["event_id"] for c in cases}
        if missing:
            # Кейс выпал из окна --days: перезапись молча сузила бы набор,
            # а старые результаты по нему тихо перестали бы считаться.
            raise SystemExit(f"--keep-ids: не вернулись event_id {sorted(missing)} "
                             f"(окно --days {args.days}?) — cases.jsonl не перезаписан")
    with_snapshot = attach_context_snapshots(args, cases)
    with_kg = reconstruct_from_kg(args, cases)
    path = out / "cases.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False, default=str) + "\n")
    with_obs = sum(1 for c in cases if c["context"] == "medic_observed")
    print(f"событий медика: {len(rows)}, кейсов: {len(cases)}, с наблюдениями: {with_obs}, "
          f"со снимком: {with_snapshot}, с реконструкцией из графа: {with_kg}, "
          f"фактов отброшено из-за совпадения с выводом медика: {leaks} → {path}")
    return 0


# --- run ------------------------------------------------------------------


def _to_incident(case: Dict[str, Any]):
    from app.models.incident import Incident

    alertname = case.get("alert_name") or (case.get("alertnames") or ["unknown"])[0]
    desc = case.get("alert_description") or ""
    summary = f"{alertname} in {case.get('namespace')} ({case.get('service_name')})"
    return Incident(
        incident_id=f"live-rca-{case['event_id']}",
        severity=case.get("severity") or "warning",
        status="firing",
        summary=summary,
        description=desc,
        namespace=case.get("namespace"),
        labels={
            "alertname": alertname,
            "namespace": case.get("namespace") or "",
            "service": case.get("service_name") or "",
        },
        annotations={"summary": summary, "description": desc},
        starts_at=str(case.get("opened_at") or case.get("started_at")),
    )


# Снимок медика — частичный: он записывал то, что счёл важным, а не весь
# namespace. Поля, которые он наполняет, помечаются partial, и правило,
# не нашедшее своего сигнала, отвечает «?», а не «не было».
_MEDIC_PARTIAL = "partial: восстановлено из наблюдений squad-medic, не полный снимок"


def apply_medic_observations(ctx: Dict[str, Any], facts: List[str]) -> Dict[str, Any]:
    ctx["k8s_summary"] = "\n".join(facts)
    ctx["k8s_events"] = observation_events(facts)
    status = dict(ctx.get("source_status") or {})
    for field in ("k8s_summary", "k8s_events", "k8s_pod_state"):
        status.setdefault(field, _MEDIC_PARTIAL)
    ctx["source_status"] = status
    return ctx


def case_summary(incident_summary: str, facts: List[str]) -> str:
    if not facts:
        return incident_summary
    return (incident_summary + "\n\nНаблюдения на момент разбора (только наблюдаемое "
            "состояние, без выводов):\n" + "\n".join(f"- {f}" for f in facts))


async def _run_case(case: Dict[str, Any], context: str) -> Dict[str, Any]:
    from app.agents.fact_critic import (FactCriticAgent, best_candidate,
                                         survivors)
    from app.agents.multi_hypothesis import MultiHypothesisAgent
    from app.diagnostics import default_engine
    from app.diagnostics.incident_ctx import build_diagnostics_ctx

    incident = _to_incident(case)
    facts = (case.get("observed_medic") or []) if context == "medic_observed" else []
    ctx = build_diagnostics_ctx(incident, analyzer_summary="", kg_session=None)
    ctx.pop("collector_results", None)
    ctx = ctx_from_kg(ctx, case.get("kg_context"))
    ctx = ctx_from_snapshot(ctx, case.get("context_snapshot"))
    if facts:
        apply_medic_observations(ctx, facts)
    store = default_engine.run(ctx)
    t0 = time.monotonic()
    hypotheses = await MultiHypothesisAgent().generate(
        incident_summary=case_summary(incident.summary, facts), facts=store
    )
    critiqued = await FactCriticAgent().critique_all(hypotheses, store)
    best = best_candidate(critiqued)

    def _cause(h) -> str:
        return str(getattr(h, "cause", "") or "")

    # Только выжившие, как у best_candidate: опровергнутая критиком гипотеза
    # в Top-3 засчитала бы модели причину, от которой пайплайн сам отказался.
    ranked = sorted(
        survivors(critiqued).items, key=lambda h: float(getattr(h, "confidence", 0.0) or 0.0), reverse=True
    )
    return {
        "event_id": case["event_id"],
        "context": context,
        "latency_s": round(time.monotonic() - t0, 1),
        "hypotheses_raw": len(hypotheses.items),
        "hypotheses_critiqued": len(critiqued.items),
        "best_cause": _cause(best) if best else None,
        "best_confidence": float(getattr(best, "confidence", 0.0) or 0.0) if best else None,
        "ranked_causes": [_cause(h) for h in ranked[:5]],
        "observed_facts": sorted(store.observed_kinds()),
        "had_snapshot": isinstance(case.get("context_snapshot"), dict),
        # Режим прогона — в "context" выше (по нему done и score by_context);
        # метка входа самого кейса — отдельно, чтобы одна не затирала другую.
        "case_context": case.get("context") or ("snapshot" if case.get("context_snapshot") else "alert_only"),
    }


async def _run_async(args) -> int:
    out = _guard_out_dir(Path(args.out))
    cases = [json.loads(ln) for ln in (out / "cases.jsonl").read_text().splitlines() if ln]
    res_path = out / "results.jsonl"
    # Ключ прогона — (кейс, режим входа): один инцидент меряется и «только
    # по алерту», и «с наблюдениями медика», и эти результаты не затирают
    # друг друга. У результатов до меток режима — alert_only.
    done: Set[tuple] = set()
    if res_path.exists():
        for ln in res_path.read_text().splitlines():
            if ln:
                r = json.loads(ln)
                done.add((r["event_id"], r.get("context", "alert_only")))
    if args.ids:
        wanted = {int(x) for x in args.ids.split(",")}
        cases = [c for c in cases if c["event_id"] in wanted]
        # Явный --ids = перепрогон: после kg-context у кейса новый вход, и
        # старый результат по одному алерту иначе остался бы в score.
        if res_path.exists():
            kept = [ln for ln in res_path.read_text().splitlines()
                    if ln and json.loads(ln)["event_id"] not in wanted]
            res_path.write_text("".join(ln + "\n" for ln in kept))
        done = {d for d in done if d[0] not in wanted}

    # auto = оба режима на ОДНИХ И ТЕХ ЖЕ кейсах: alert_only для всех,
    # medic_observed — где наблюдения есть. Иначе by_context сравнивал бы
    # разные популяции инцидентов, а не эффект наблюдений.
    # getattr: вызывающие код программно (тесты) могут собрать args без --context.
    context_mode = getattr(args, "context", "auto")

    def modes(c: Dict[str, Any]) -> List[str]:
        if context_mode == "auto":
            return ["alert_only"] + (["medic_observed"] if c.get("observed_medic") else [])
        if context_mode == "medic_observed" and not c.get("observed_medic"):
            return []
        return [context_mode]

    work = [(c, m) for c in cases for m in modes(c) if (c["event_id"], m) not in done]
    todo = work[: args.limit]
    print(f"кейсов всего {len(cases)}, прогнано {len(done)}, в этом заходе {len(todo)}")
    for c, m in todo:
        try:
            r = await _run_case(c, m)
        except Exception as e:  # кейс, а не прогон: остальные должны пройти
            r = {"event_id": c["event_id"], "context": m, "error": f"{type(e).__name__}: {e}"}
        with res_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"  #{c['event_id']} [{m}]: {'ошибка' if r.get('error') else 'ok'} "
              f"{r.get('latency_s', '-')}s")
    return 0


def cmd_run(args) -> int:
    os.environ.setdefault("LLM_BACKEND", "claude_cli")
    return asyncio.run(_run_async(args))


# --- score ----------------------------------------------------------------


def score(cases: List[Dict[str, Any]], results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Метрики всего прогона и отдельно по режиму входа.

    Смешивать режимы в одну цифру нельзя: «только алерт» почти всегда даёт
    отказ, и общий abstain-rate говорил бы о составе выборки, а не о модели.
    """
    summary = _score_block(cases, results)
    contexts = sorted({r.get("context", "alert_only") for r in results})
    summary["by_context"] = {
        ctx: _score_block(cases, [r for r in results if r.get("context", "alert_only") == ctx])
        for ctx in contexts
    }
    return summary


def _score_block(cases: List[Dict[str, Any]], results: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_id = {c["event_id"]: c for c in cases}
    rows: List[Dict[str, Any]] = []
    for r in results:
        c = by_id.get(r["event_id"])
        if not c:
            continue
        primary = c.get("expected_primary")
        expected = {primary} if primary else set()
        if r.get("error"):
            rows.append({"error": True, "latency_s": 0.0})
            continue
        best = classify(r.get("best_cause"))
        top3: Set[str] = set()
        for cause in (r.get("ranked_causes") or [])[:3]:
            top3 |= classify(cause)
        row: Dict[str, Any] = {
            "error": False,
            "gradable": bool(expected),
            "abstained": r.get("best_cause") is None,
            "top1": bool(expected & best),
            "top3": bool(expected & top3),
            "unclassified_answer": r.get("best_cause") is not None and not best,
            "latency_s": r.get("latency_s") or 0.0,
        }
        rows.append(row)
    ok = [x for x in rows if not x["error"]]
    graded = [x for x in ok if x["gradable"]]

    def rate(xs, key):
        return round(sum(1 for x in xs if x[key]) / len(xs), 3) if xs else None

    lat = sorted(x["latency_s"] for x in ok)
    return {
        "cases_run": len(rows),
        "errors": len(rows) - len(ok),
        "gradable": len(graded),
        "top1": rate(graded, "top1"),
        "top3": rate(graded, "top3"),
        "abstention_rate": rate(ok, "abstained"),
        "unclassified_answer_rate": rate(ok, "unclassified_answer"),
        "latency_p50_s": lat[len(lat) // 2] if lat else None,
        "latency_max_s": lat[-1] if lat else None,
        "note": ("попадание = первичный класс причины медика среди классов ответа "
                 "модели (регулярки CAUSE_CLASSES); alert_only — только алерт, "
                 "medic_observed — алерт + наблюдения медика без выводов"),
    }


def cmd_score(args) -> int:
    out = _guard_out_dir(Path(args.out))
    cases = [json.loads(ln) for ln in (out / "cases.jsonl").read_text().splitlines() if ln]
    results = [json.loads(ln) for ln in (out / "results.jsonl").read_text().splitlines() if ln]
    summary = score(cases, results)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    for k, v in summary.items():
        if k == "by_context":
            for ctx, block in v.items():
                print(f"--- {ctx}")
                for bk, bv in block.items():
                    if bk != "note":
                        print(f"  {bk:24} {bv}")
            continue
        print(f"{k:26} {v}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="каталог датасета (вне репо)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export")
    ex.add_argument("--days", type=int, default=30)
    ex.add_argument("--keep-ids", action="store_true",
                    help="перевыгрузить ровно те кейсы, что уже лежат в cases.jsonl")
    kc = sub.add_parser("kg-context", help="дописать снимки/реконструкцию в готовый cases.jsonl")
    for p in (ex, kc):
        p.add_argument("--context", default="lastoasisgame-local")
        p.add_argument("--namespace", default="sre-ai")
        p.add_argument("--db-user", default="sre_ai")
        p.add_argument("--db-name", default="sre_copilot")
    rn = sub.add_parser("run")
    rn.add_argument("--limit", type=int, default=3, help="кейсов за заход (каждый ≈ 15 вызовов LLM)")
    rn.add_argument("--context", choices=("auto", "alert_only", "medic_observed"), default="auto",
                    help="вход модели: auto — по метке кейса")
    rn.add_argument("--ids", default="", help="event_id через запятую — прогнать только их")
    sub.add_parser("score")
    args = ap.parse_args()
    return {"export": cmd_export, "kg-context": cmd_kg_context, "run": cmd_run,
            "score": cmd_score}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
