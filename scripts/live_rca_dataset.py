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
          read-only) → cases.jsonl, у каждого кейса — контекст графа на начало
          разбора медика. Только чтение.
  kg-context  перечитать контекст графа для готового cases.jsonl. Только чтение.
  run     прогон N кейсов через модель → results.jsonl (дописывает, уже
          прогнанные пропускает — прогон можно дробить).
  score   метрики по results.jsonl → summary.json + таблица.
  facts   покрытие правил по режимам входа, без LLM.

ДАННЫЕ — ТОЛЬКО ВНЕ РЕПО. Репозиторий публичный, а в кейсах живые namespace-ы,
имена сервисов, текст алертов и выводы медика. По умолчанию всё пишется в
`~/.cache/sre-ai-copilot/live-rca/<дата>/`; путь внутри репо скрипт отвергает.

Оценка — грубая и честная: причину медика и причину модели раскладываем по
одним и тем же классам (`CAUSE_CLASSES`, регулярки RU+EN), попадание — это
пересечение классов. Это нижняя оценка: модель могла назвать ту же причину
словами, которых нет в регулярках.

Вход кейса собирается ТЕМ ЖЕ кодом, что у прода: контекст графа —
app/context/kg_incident_context.fetch_kg_incident_context (те же запросы,
отрендеренные для psql), раскладка в ctx правил — build_diagnostics_ctx,
текст для модели — kg_context_prompt. Своей реконструкции в скрипте нет:
датасет меряет ровно то, что увидел бы пайплайн. Медик — не отдельный вход, а
источник squad-medic внутри контекста графа (его наблюдения из
kg_remediation_events, без выводов). Режимы (`MODES`): alert_only, kg,
kg_no_medic — метрики по ним раздельно.

Target кейса — не сервис ближайшего инцидента, а сломанный workload: тот, у
кого в окне больше всего «плохих» событий подов (`select_targets` сборщика). Ближайший
инцидент стендового разбора медика — чаще всего шумовой
KubeDeploymentGenerationMismatch от Rancher-churn на сервисе с ingress
(analytics/admin), и привязка к нему уводила правила в «чужой workload».

Исход (`outcome_confirmed`): до 24.09.2026 08:56 UTC (external/mcp!115)
`fixed=true` значило «медик что-то применил», а не «стенд здоров» — 514 из 521
таких прогонов стенд остался больным, и их root_cause — догадка, а не ответ.
Подтверждённым считается кейс, где медик сам видел стенд здоровым
(`still_unhealthy=false`), или граф через 1–2 ч после разбора затих: плохие
события подов сквада кончились, алерты резолвнулись. score считает метрики
отдельно по подтверждённым.
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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

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
# Наблюдения squad-medic (что он ВИДЕЛ, без выводов) извлекаются в app и
# живут в графе (kg_remediation_events.observations) как источник squad-medic
# контекста инцидента — датасет читает их через тот же сборщик, что и прод.
# Здесь — только страховка оценки от утечки ответа (answer_leak).
from app.context.medic_observations import answer_leak  # noqa: E402

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
  SELECT e.id AS event_id, e.started_at, e.finished_at, e.outcome, e.still_unhealthy,
         e.root_cause, e.summary, e.applied, e.manual, e.gaps, e.extras,
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
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        # Сбой обогащения не роняет выгрузку: кейс без него — прежний вход.
        print("psql: таймаут 120 с, порция пропущена", file=sys.stderr)
        return None
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
    # События/деплои/алерты в снимке — ссылки (kg_refs), их данные несёт
    # kg_context кейса; здесь — только то, чего в графе нет.
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


# --- контекст графа: тот же сборщик, что у прода ----------------------------
#
# Своей реконструкции здесь больше нет: кейс получает контекст графа от
# app/context/kg_incident_context.fetch_kg_incident_context — те же запросы
# (объекты Select, отрендеренные в SQL для psql), тот же point-in-time на
# начало разбора медика, тот же выбор target-а. Наблюдения squad-medic
# приходят как источник внутри этого контекста, из графа.


def _run_psql(args) -> Callable[[str], Optional[str]]:
    """Транспорт для PsqlReader: `kubectl exec psql` в под postgres-0.

    Прямой kubectl — только здесь, в скрипте: app/ ходит в кластер через
    kubectl_breaker, а в боевую БД — сессией.
    """
    def run(sql: str) -> Optional[str]:
        cmd = ["kubectl", "--context", args.context, "-n", args.namespace, "exec",
               "postgres-0", "--", "psql", "-U", args.db_user, "-d", args.db_name,
               "-At", "-v", "ON_ERROR_STOP=1", "-c", sql]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            print("psql: таймаут 180 с", file=sys.stderr)
            return None
        if proc.returncode != 0:
            print(proc.stderr[-800:], file=sys.stderr)
            return None
        return proc.stdout
    return run


def case_scope(case: Dict[str, Any]):
    """Скоуп кейса: граф на НАЧАЛО разбора медика. Сам разбор из истории
    исключён (его итог — ответ кейса), выводы прошлых разборов — тоже
    (в проде это законное «так уже было», в оценке — подсказка)."""
    from app.context.kg_incident_context import KGScope

    return KGScope(
        namespace=case.get("namespace") or "",
        service=case.get("service_name") or None,
        alertname=case.get("alert_name") or None,
        as_of=_parse_ts(case.get("started_at")) or datetime.now(timezone.utc),
        with_conclusions=False,
        exclude_remediation_ids=(int(case["event_id"]),) if case.get("event_id") else (),
    )


def fetch_kg_contexts(args, cases: List[Dict[str, Any]]) -> Dict[str, int]:
    """Дописать в кейсы `kg_context` (граф на момент разбора) и target.

    Наблюдения squad-medic внутри контекста проходят страховку answer_leak
    против root_cause кейса: наблюдение, процитированное и в выводе медика,
    из входа выбрасывается (в проде того же фильтра нет — там это не ответ).
    """
    from app.context.kg_incident_context import (PsqlReader,
                                                 fetch_kg_incident_context)

    reader = PsqlReader(_run_psql(args))
    stats = {"with_kg": 0, "retargeted": 0, "leaks": 0, "with_medic": 0}
    for c in cases:
        try:
            kgc = fetch_kg_incident_context(reader, case_scope(c))
        except Exception as e:  # кейс, а не выгрузка: остальные должны пройти
            print(f"  #{c.get('event_id')}: граф не ответил: {type(e).__name__}", file=sys.stderr)
            continue
        for o in kgc.get("medic_observations") or []:
            facts = o.get("facts") or []
            clean = [f for f in facts if not answer_leak([f], c.get("root_cause"))]
            stats["leaks"] += len(facts) - len(clean)
            o["facts"] = clean
        c["kg_context"] = kgc
        stats["with_kg"] += 1
        if any(o.get("facts") for o in kgc.get("medic_observations") or []):
            stats["with_medic"] += 1
        if set_case_target(c):
            stats["retargeted"] += 1
    return stats


def set_case_target(case: Dict[str, Any]) -> bool:
    """`targets`/`target_workload` кейса из контекста графа; сервис инцидента
    — `incident_service`. True — target сменился."""
    from app.diagnostics.rules.base import same_workload

    case.setdefault("incident_service", case.get("service_name"))
    targets = (case.get("kg_context") or {}).get("targets") or []
    case["targets"] = targets
    if not targets:
        case.pop("target_workload", None)
        case.pop("target_namespace", None)
        return False
    case["target_workload"] = targets[0]["workload"]
    case["target_namespace"] = targets[0]["namespace"]
    return not same_workload(case["target_workload"], case.get("incident_service") or "")


def target_event_share(case: Dict[str, Any], target: Optional[str]) -> Optional[float]:
    """Доля событий подов окна, принадлежащих target-у: мера того, насколько
    вход кейса вообще про его target."""
    from app.diagnostics.rules.base import same_workload

    evs = (case.get("kg_context") or {}).get("pod_events") or []
    if not evs or not target:
        return None
    return sum(1 for e in evs if same_workload(e.get("pod") or "", target)) / len(evs)


def _parse_ts(v: Any) -> Optional[datetime]:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# Отсечка семантики `fixed` у медика (external/mcp!115 раскатан 24.09.2026 08:56
# UTC): после неё fixed=true = «стенд здоров», до — «что-то применил». Та же
# константа, что в app/context/kg_incident_context (история для модели).
MEDIC_FIXED_SEMANTICS_CUTOVER = datetime(2026, 9, 24, 8, 56, tzinfo=timezone.utc)

# Затих ли сквад через 1–2 ч после разбора. Только метка исхода — во вход
# кейса отсюда не попадает ничего: это было бы будущее относительно инцидента.
_OUTCOME_SQL = """
SET TRANSACTION READ ONLY;
SELECT to_jsonb(t) FROM (
  SELECT e.id AS event_id,
    (now() >= coalesce(e.finished_at, e.started_at) + interval '2 hours') AS observable,
    -- Покрытие: граф видел поломку ДО разбора. Без этого нули ниже могут
    -- значить «синк не писал сквад / строки ушли по retention», а не «затих».
    (SELECT count(*) FROM kg_pod_events pe
      WHERE pe.namespace = ANY(sc.ns) AND pe.type = 'Warning'
        AND pe.reason = ANY('{{{reasons}}}'::text[])
        AND pe.first_seen BETWEEN e.started_at - interval '7 days' AND e.started_at
        AND coalesce(pe.last_seen, pe.first_seen) >= e.started_at - interval '2 hours'
    ) AS bad_events_before,
    (SELECT count(*) FROM kg_pod_events pe
      WHERE pe.namespace = ANY(sc.ns) AND pe.type = 'Warning'
        AND pe.reason = ANY('{{{reasons}}}'::text[])
        AND pe.first_seen BETWEEN e.started_at - interval '7 days'
                              AND coalesce(e.finished_at, e.started_at) + interval '2 hours'
        AND coalesce(pe.last_seen, pe.first_seen)
            >= coalesce(e.finished_at, e.started_at) + interval '1 hour') AS bad_events_after,
    (SELECT count(*) FROM kg_alerts a JOIN kg_services s ON s.id = a.service_id
      WHERE s.namespace = ANY(sc.ns)
        AND a.alertname <> ALL('{{{noise}}}'::text[])
        AND a.fired_at BETWEEN e.started_at - interval '2 hours'
                           AND coalesce(e.finished_at, e.started_at) + interval '2 hours'
        AND (a.resolved_at IS NULL
             OR a.resolved_at > coalesce(e.finished_at, e.started_at) + interval '2 hours')
    ) AS alerts_open_after
  FROM kg_remediation_events e
  JOIN kg_incidents i ON i.id = e.incident_id
  CROSS JOIN LATERAL (
    SELECT array_append(coalesce(
             (SELECT array_agg(DISTINCT s2.namespace) FROM kg_services s2
              WHERE s2.namespace LIKE p.scope), '{{}}'::text[]), i.namespace) AS ns
    FROM (SELECT coalesce(substring(i.namespace from '^(squad-[^-]+-)') || '%',
                          i.namespace) AS scope) p
  ) sc
  WHERE e.id IN ({ids})
) t;
"""


def fetch_kg_outcomes(args, ids: List[int], chunk: int = 60) -> Dict[int, Dict[str, Any]]:
    """Исход по графу для событий медика; сбой запроса = исход неизвестен."""
    from app.context.kg_incident_context import BAD_POD_REASONS, NOISE_ALERTS

    out: Dict[int, Dict[str, Any]] = {}
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        sql = _OUTCOME_SQL.format(ids=",".join(map(str, part)),
                                  reasons=",".join(sorted(BAD_POD_REASONS)),
                                  noise=",".join(sorted(NOISE_ALERTS)))
        for r in _psql_rows(args, sql) or []:
            out[int(r["event_id"])] = r
    return out


def outcome_evidence(row: Dict[str, Any], kg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Подтверждён ли исход кейса и чем.

    medic_healthy — медик сам видел стенд здоровым после разбора
    (still_unhealthy=false / outcome=fixed; после отсечки !115 это же значит
    fixed=true). kg_quiet — граф видел поломку до разбора, а через 1–2 ч
    после него у сквада нет плохих Warning-событий и открытых не-шумовых
    алертов; None — рано, граф не ответил или поломки в графе не было вовсе
    (нули тогда не доказывают тишину: синк мог сквад не писать).
    """
    started = _parse_ts(row.get("started_at"))
    after_cutover = bool(started and started >= MEDIC_FIXED_SEMANTICS_CUTOVER)
    medic_healthy = (row.get("still_unhealthy") is False or row.get("outcome") == "fixed"
                     or (after_cutover and bool(row.get("fixed", True))))
    kg_quiet: Optional[bool] = None
    if kg and kg.get("observable") and int(kg.get("bad_events_before") or 0) > 0:
        kg_quiet = (int(kg.get("bad_events_after") or 0) == 0
                    and int(kg.get("alerts_open_after") or 0) == 0)
    return {"medic_healthy": medic_healthy, "kg_quiet": kg_quiet,
            "fixed_semantics": "healthy" if after_cutover else "applied_something",
            "confirmed": bool(medic_healthy or kg_quiet)}


def cmd_kg_context(args) -> int:
    """Ретроактивно: перечитать контекст графа для УЖЕ собранного cases.jsonl,
    не пересобирая выборку (прогнанные results остаются валидны по event_id;
    перепрогон кейса с новым входом — `run --ids`)."""
    out = _guard_out_dir(Path(args.out))
    path = out / "cases.jsonl"
    cases = [json.loads(ln) for ln in path.read_text().splitlines() if ln]
    with_snapshot = attach_context_snapshots(args, cases)
    st = fetch_kg_contexts(args, cases)
    with path.open("w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False, default=str) + "\n")
    print(f"кейсов: {len(cases)}, со снимком: {with_snapshot}, с контекстом графа: "
          f"{st['with_kg']} (из них с наблюдениями squad-medic: {st['with_medic']}), "
          f"сменили target: {st['retargeted']}, наблюдений отброшено страховкой: "
          f"{st['leaks']} → {path}")
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

    if keep is not None:
        rows = [r for r in rows if r["event_id"] in keep]
    # Исход — ДО дедупа: из серии разборов одного стенда с той же причиной
    # в датасет должен попасть подтверждённый (стенд потом правда выздоровел),
    # а не самый свежий «что-то применил».
    kg_outcomes = fetch_kg_outcomes(args, [int(r["event_id"]) for r in rows])
    for r in rows:
        ev = outcome_evidence(r, kg_outcomes.get(int(r["event_id"])))
        r["outcome_evidence"] = ev
        r["outcome_confirmed"] = ev["confirmed"]
    # sorted стабилен: внутри «подтверждённых» и «нет» сохраняется порядок
    # started_at DESC из SQL.
    rows = sorted(rows, key=lambda r: not r["outcome_confirmed"])

    # Медик часто разбирает один и тот же стенд несколько раз подряд с той же
    # причиной: 114 событий KubeContainerWaiting — почти все «retention снёс
    # теги». Без дедупа датасет меряет один случай сотню раз.
    seen: Set[tuple] = set()
    cases = []
    for r in rows:
        alertnames = r.get("alertnames") or []
        labels = sorted(classify(r.get("root_cause")))
        primary = primary_class(r.get("root_cause"))
        key = (tuple(alertnames), primary, r.get("namespace"))
        if keep is None and key in seen:
            continue
        seen.add(key)
        r["expected_classes"] = labels
        r["expected_primary"] = primary
        # Сырые поля медика в кейс не идут: его наблюдения приходят из графа
        # (источник squad-medic контекста), а выводы в applied/manual/gaps —
        # лишний шанс протечь во вход. root_cause остаётся — это ответ кейса.
        for k in ("applied", "manual", "gaps", "extras", "summary"):
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
    st = fetch_kg_contexts(args, cases)
    path = out / "cases.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False, default=str) + "\n")
    confirmed = sum(1 for c in cases if c.get("outcome_confirmed"))
    print(f"событий медика: {len(rows)}, кейсов: {len(cases)}, со снимком: {with_snapshot}, "
          f"с контекстом графа: {st['with_kg']} (с наблюдениями squad-medic: "
          f"{st['with_medic']}), сменили target: {st['retargeted']}, исход подтверждён: "
          f"{confirmed}, наблюдений отброшено страховкой answer_leak: {st['leaks']} → {path}")
    return 0


# --- run ------------------------------------------------------------------


def _to_incident(case: Dict[str, Any]):
    from app.context.kg_incident_context import NOISE_ALERTS
    from app.models.incident import Incident

    alertname = case.get("alert_name") or (case.get("alertnames") or ["unknown"])[0]
    desc = case.get("alert_description") or ""
    # Target — сломанный workload из select_targets; сервис ближайшего
    # инцидента — только если графу нечего сказать и инцидент не шумовой:
    # GenerationMismatch от Rancher-churn указывает на здоровый сервис с
    # ingress, и пустой target (масштаб namespace) честнее чужого.
    service = case.get("target_workload") or ""
    if not service and alertname not in NOISE_ALERTS:
        service = case.get("service_name") or ""
    namespace = case.get("target_namespace") or case.get("namespace") or ""
    summary = f"{alertname} in {namespace} ({service})"
    return Incident(
        incident_id=f"live-rca-{case['event_id']}",
        severity=case.get("severity") or "warning",
        status="firing",
        summary=summary,
        description=desc,
        namespace=namespace,
        labels={
            "alertname": alertname,
            "namespace": namespace,
            "service": service,
        },
        annotations={"summary": summary, "description": desc},
        starts_at=str(case.get("opened_at") or case.get("started_at")),
    )


# --- ctx кейса: тот же build_diagnostics_ctx, что у прода --------------------

# Режимы входа: какой источник кейса подаётся модели. Один инцидент меряется
# во всех режимах, для которых у него есть данные, — иначе by_context
# сравнивал бы разные популяции, а не эффект источника.
#   alert_only   только алерт;
#   kg           контекст графа на начало разбора, как у прода: события подов
#                и Job-ы сквада, деплои, история, наблюдения squad-medic;
#   kg_no_medic  то же без источника squad-medic — сколько даёт медик.
MODES = ("alert_only", "kg", "kg_no_medic")

# Поля ctx, которые в проде наполняют ЖИВЫЕ источники (снимок K8sFacts,
# VictoriaMetrics, TeamCity) — у кейса датасета их нет. Пустое поле без
# пометки читалось бы как «опрошено, пусто», и OOMKilledRule по кейсу без
# логов уверенно отвечал бы «OOM не было».
RULE_SOURCE_FIELDS = ("k8s_events", "k8s_summary", "logs_summary", "k8s_pod_state",
                      "metrics_summary", "recent_deployments", "upstream_alerts")
NOT_RECONSTRUCTED = "not_reconstructed"
_NOT_RECONSTRUCTED_REASON = f"{NOT_RECONSTRUCTED}: в кейсе датасета этого источника нет"


def _has_medic(case: Dict[str, Any]) -> bool:
    kgc = case.get("kg_context") or {}
    return any(o.get("facts") for o in kgc.get("medic_observations") or [])


def available_modes(case: Dict[str, Any]) -> List[str]:
    """Режимы, для которых у кейса есть вход. alert_only — всегда.

    Снимок контекста считается входом «графового» режима наравне с
    контекстом графа: кейс со снимком, но без строк графа в окне, иначе терял
    бы как раз то живое состояние подов, ради которого снимок пишется.
    """
    has_kg = (isinstance(case.get("kg_context"), dict)
              or isinstance(case.get("context_snapshot"), dict))
    modes = ["alert_only"]
    if has_kg:
        modes.append("kg")
        if _has_medic(case):
            modes.append("kg_no_medic")
    return modes


def build_case_ctx(case: Dict[str, Any], mode: str) -> Dict[str, Any]:
    """ctx правил для кейса в режиме входа — ТЕМ ЖЕ кодом, что у прода.

    `build_diagnostics_ctx(kg_context=...)` раскладывает сохранённый в кейсе
    контекст графа той же `apply_kg_context`, что раскладывает собранный в
    проде. alert_only граф не получает вовсе. Всё, что в проде даёт живой
    источник, а у кейса отсутствует, — в source_status: правило ответит «?».
    """
    from app.diagnostics.incident_ctx import build_diagnostics_ctx

    if mode not in MODES:
        raise ValueError(f"неизвестный режим входа: {mode}")
    kgc = case.get("kg_context") if mode != "alert_only" else None
    ctx = build_diagnostics_ctx(_to_incident(case), analyzer_summary="", kg_session=None,
                                kg_context=kgc, include_medic=(mode == "kg"))
    ctx.pop("collector_results", None)
    status = dict(ctx.get("source_status") or {})
    for field in RULE_SOURCE_FIELDS:
        if not ctx.get(field):
            status.setdefault(field, _NOT_RECONSTRUCTED_REASON)
    ctx["source_status"] = status
    if mode != "alert_only":
        ctx = ctx_from_snapshot(ctx, case.get("context_snapshot"))
    return ctx


def rule_facts(case: Dict[str, Any], mode: str) -> Dict[str, Any]:
    """Что правила извлекают из кейса в режиме — без LLM, бесплатно."""
    from app.diagnostics import default_engine

    store = default_engine.run(build_case_ctx(case, mode))
    by_verdict: Dict[str, List[str]] = {}
    for f in store.facts:
        v = getattr(f.verdict, "value", f.verdict) or "?"
        by_verdict.setdefault(str(v), []).append(f.kind)
    return {"observed": sorted(store.observed_kinds()),
            "by_verdict": {k: sorted(set(v)) for k, v in by_verdict.items()}}


def cmd_facts(args) -> int:
    """Покрытие правил по режимам входа: сколько кейсов дают хоть один
    наблюдённый факт и какие kind-ы. Меряет вход до того, как тратить LLM."""
    # Settings валидируется при импорте app.*; LLM здесь не зовётся.
    os.environ.setdefault("LLM_BACKEND", "claude_cli")
    out = _guard_out_dir(Path(args.out))
    cases = [json.loads(ln) for ln in (out / "cases.jsonl").read_text().splitlines() if ln]
    report: Dict[str, Any] = {}
    for mode in MODES:
        rows = [rule_facts(c, mode) for c in cases if mode in available_modes(c)]
        kinds: Dict[str, int] = {}
        for r in rows:
            for k in r["observed"]:
                kinds[k] = kinds.get(k, 0) + 1
        report[mode] = {
            "cases": len(rows),
            "with_observed_fact": sum(1 for r in rows if r["observed"]),
            "observed_kinds": dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
        }
    (out / "facts_coverage.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    for mode, r in report.items():
        print(f"{mode:17} кейсов {r['cases']:3}  с фактом {r['with_observed_fact']:3}  {r['observed_kinds']}")
    return 0


def case_prompt(case: Dict[str, Any], mode: str) -> str:
    """Текст инцидента для модели — как в пайплайне: сводка алерта плюс блок
    контекста графа (`kg_context_prompt`), а не своя сборка скрипта."""
    from app.context.kg_incident_context import kg_context_prompt

    summary = _to_incident(case).summary
    if mode == "alert_only":
        return summary
    block = kg_context_prompt(case.get("kg_context"), include_medic=(mode == "kg"))
    return f"{summary}\n\n{block}" if block else summary


async def _run_case(case: Dict[str, Any], context: str) -> Dict[str, Any]:
    from app.agents.fact_critic import (FactCriticAgent, best_candidate,
                                         survivors)
    from app.agents.multi_hypothesis import MultiHypothesisAgent
    from app.diagnostics import default_engine

    store = default_engine.run(build_case_ctx(case, context))
    t0 = time.monotonic()
    hypotheses = await MultiHypothesisAgent().generate(
        incident_summary=case_prompt(case, context), facts=store
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

    # auto = все режимы на ОДНИХ И ТЕХ ЖЕ кейсах: alert_only для всех,
    # остальные — где у кейса есть соответствующий вход. Иначе by_context
    # сравнивал бы разные популяции инцидентов, а не эффект источника.
    # getattr: вызывающие код программно (тесты) могут собрать args без --context.
    context_mode = getattr(args, "context", "auto")

    def modes(c: Dict[str, Any]) -> List[str]:
        avail = available_modes(c)
        if context_mode == "auto":
            return avail
        return [context_mode] if context_mode in avail else []

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
    # root_cause у неподтверждённых — догадка медика по стенду, который так и
    # остался больным; «попадание» в неё — слабый сигнал. Цифры раздельно.
    confirmed_ids = {c["event_id"] for c in cases if c.get("outcome_confirmed")}
    summary["by_outcome"] = {
        label: {
            ctx: _score_block(cases, [r for r in results
                                      if r.get("context", "alert_only") == ctx
                                      and (r["event_id"] in confirmed_ids) == want])
            for ctx in contexts
        }
        for label, want in (("confirmed", True), ("unconfirmed", False))
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
                 "kg — контекст графа на начало разбора (с наблюдениями squad-medic), "
                 "kg_no_medic — то же без squad-medic"),
    }


def cmd_score(args) -> int:
    out = _guard_out_dir(Path(args.out))
    cases = [json.loads(ln) for ln in (out / "cases.jsonl").read_text().splitlines() if ln]
    results = [json.loads(ln) for ln in (out / "results.jsonl").read_text().splitlines() if ln]
    summary = score(cases, results)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    def _print_blocks(title: str, blocks: Dict[str, Any]) -> None:
        for ctx, block in blocks.items():
            print(f"--- {title}{ctx}")
            for bk, bv in block.items():
                if bk != "note":
                    print(f"  {bk:24} {bv}")

    for k, v in summary.items():
        if k == "by_context":
            _print_blocks("", v)
            continue
        if k == "by_outcome":
            for label, blocks in v.items():
                _print_blocks(f"{label} / ", blocks)
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
    rn.add_argument("--context", choices=("auto",) + MODES, default="auto",
                    help="вход модели: auto — все режимы, для которых у кейса есть данные")
    rn.add_argument("--ids", default="", help="event_id через запятую — прогнать только их")
    sub.add_parser("score")
    sub.add_parser("facts", help="покрытие правил по режимам входа, без LLM")
    args = ap.parse_args()
    return {"export": cmd_export, "kg-context": cmd_kg_context, "run": cmd_run,
            "score": cmd_score, "facts": cmd_facts}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
