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
словами, которых нет в регулярках. Вход — алерт, плюс снимок контекста
инцидента (`analysis.context_snapshot`), если пайплайн его записал: `export`
подтягивает снимок, `run` накладывает его на ctx правил. У кейсов без снимка
цифры меряют «разбор по алерту», а не «разбор с полным контекстом» — в
results это видно по `had_snapshot`.
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
  WHERE e.id IN ({ids}) AND r.created_at <= e.started_at
    AND jsonb_typeof(to_jsonb(r.analysis) -> 'context_snapshot') = 'object'
  ORDER BY e.id, r.created_at DESC
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
  SELECT e.id AS event_id,
    (SELECT coalesce(jsonb_agg(x), '[]'::jsonb) FROM (
       -- Последнее событие на (под, reason): иначе 40 мест съедают сотни
       -- одинаковых Unhealthy от readiness-пробы, а BackOff/OOMKilled не влезают.
       SELECT * FROM (
         SELECT DISTINCT ON (pe.pod_name, pe.reason)
                pe.pod_name AS pod, pe.type, pe.reason, left(pe.message, 300) AS message,
                pe.count, pe.first_seen, pe.last_seen
         FROM kg_pod_events pe
         WHERE pe.namespace = i.namespace
           AND coalesce(pe.last_seen, pe.first_seen) >= e.started_at - interval '2 hours'
           AND pe.first_seen <= e.started_at
         ORDER BY pe.pod_name, pe.reason, coalesce(pe.last_seen, pe.first_seen) DESC) d
       ORDER BY (d.type = 'Warning') DESC, coalesce(d.last_seen, d.first_seen) DESC
       LIMIT 40) x) AS pod_events,
    (SELECT coalesce(jsonb_agg(x), '[]'::jsonb) FROM (
       SELECT s.name AS service, a.alertname, a.severity, a.fired_at, a.resolved_at
       FROM kg_alerts a JOIN kg_services s ON s.id = a.service_id
       WHERE s.namespace = i.namespace
         AND a.fired_at BETWEEN e.started_at - interval '2 hours' AND e.started_at
       ORDER BY a.fired_at DESC LIMIT 20) x) AS alerts,
    (SELECT coalesce(jsonb_agg(x), '[]'::jsonb) FROM (
       SELECT s.name AS service, d.status, d.buildtype_id, d.started_at, d.finished_at
       FROM kg_deployments d JOIN kg_services s ON s.id = d.service_id
       WHERE s.namespace = i.namespace
         AND d.started_at BETWEEN e.started_at - interval '6 hours' AND e.started_at
       ORDER BY d.started_at DESC LIMIT 10) x) AS deployments
  FROM kg_remediation_events e
  JOIN kg_incidents i ON i.id = e.incident_id
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
        if not r or not (r.get("pod_events") or r.get("alerts") or r.get("deployments")):
            continue
        for ev in r.get("pod_events") or []:
            if ev.get("message"):
                ev["message"] = redact_pii(ev["message"], max_len=300)
        c["kg_context"] = {k: r.get(k) or [] for k in ("pod_events", "alerts", "deployments")}
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
             "count": e.get("count"), "pod": e.get("pod"),
             "last_timestamp": e.get("last_seen") or e.get("first_seen")}
            for e in kg["pod_events"]
        ]
    if kg.get("deployments"):
        ctx["recent_deployments"] = [
            {"name": d.get("service") or d.get("buildtype_id") or "deploy",
             "ts": d.get("finished_at") or d.get("started_at"), "status": d.get("status"),
             "buildtype_id": d.get("buildtype_id"), "attribution_scope": "namespace"}
            for d in kg["deployments"]
        ]
    # Соседние алерты графа — не upstream по рёбрам зависимостей (правило
    # UpstreamDegraded ждёт edge_kind), поэтому идут в описание, не в правило.
    if kg.get("alerts"):
        names = sorted({a.get("alertname") for a in kg["alerts"] if a.get("alertname")})
        ctx["description"] = ((ctx.get("description") or "")
                              + f"\nАлерты namespace за 2ч: {', '.join(names)}").strip()
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
        if key in seen:
            continue
        seen.add(key)
        r["expected_classes"] = labels
        r["expected_primary"] = primary
        cases.append(r)
    with_snapshot = attach_context_snapshots(args, cases)
    with_kg = reconstruct_from_kg(args, cases)
    path = out / "cases.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False, default=str) + "\n")
    print(f"событий медика: {len(rows)}, после дедупа: {len(cases)}, "
          f"со снимком: {with_snapshot}, с реконструкцией из графа: {with_kg} → {path}")
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


async def _run_case(case: Dict[str, Any]) -> Dict[str, Any]:
    from app.agents.fact_critic import (FactCriticAgent, best_candidate,
                                         survivors)
    from app.agents.multi_hypothesis import MultiHypothesisAgent
    from app.diagnostics import default_engine
    from app.diagnostics.incident_ctx import build_diagnostics_ctx

    incident = _to_incident(case)
    ctx = build_diagnostics_ctx(incident, analyzer_summary="", kg_session=None)
    ctx.pop("collector_results", None)
    ctx = ctx_from_kg(ctx, case.get("kg_context"))
    ctx = ctx_from_snapshot(ctx, case.get("context_snapshot"))
    store = default_engine.run(ctx)
    t0 = time.monotonic()
    hypotheses = await MultiHypothesisAgent().generate(
        incident_summary=incident.summary, facts=store
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
        "latency_s": round(time.monotonic() - t0, 1),
        "hypotheses_raw": len(hypotheses.items),
        "hypotheses_critiqued": len(critiqued.items),
        "best_cause": _cause(best) if best else None,
        "best_confidence": float(getattr(best, "confidence", 0.0) or 0.0) if best else None,
        "ranked_causes": [_cause(h) for h in ranked[:5]],
        "observed_facts": sorted(store.observed_kinds()),
        "had_snapshot": isinstance(case.get("context_snapshot"), dict),
        "context": case.get("context") or ("snapshot" if case.get("context_snapshot") else "alert_only"),
    }


async def _run_async(args) -> int:
    out = _guard_out_dir(Path(args.out))
    cases = [json.loads(ln) for ln in (out / "cases.jsonl").read_text().splitlines() if ln]
    res_path = out / "results.jsonl"
    done = set()
    if res_path.exists():
        done = {json.loads(ln)["event_id"] for ln in res_path.read_text().splitlines() if ln}
    if args.ids:
        wanted = {int(x) for x in args.ids.split(",")}
        cases = [c for c in cases if c["event_id"] in wanted]
    todo = [c for c in cases if c["event_id"] not in done][: args.limit]
    print(f"кейсов всего {len(cases)}, прогнано {len(done)}, в этом заходе {len(todo)}")
    for c in todo:
        try:
            r = await _run_case(c)
        except Exception as e:  # кейс, а не прогон: остальные должны пройти
            r = {"event_id": c["event_id"], "error": f"{type(e).__name__}: {e}"}
        with res_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"  #{c['event_id']}: {'ошибка' if r.get('error') else 'ok'} "
              f"{r.get('latency_s', '-')}s")
    return 0


def cmd_run(args) -> int:
    os.environ.setdefault("LLM_BACKEND", "claude_cli")
    return asyncio.run(_run_async(args))


# --- score ----------------------------------------------------------------


def score(cases: List[Dict[str, Any]], results: List[Dict[str, Any]]) -> Dict[str, Any]:
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
                 "модели (регулярки CAUSE_CLASSES); вход — только алерт, без снимка кластера"),
    }


def cmd_score(args) -> int:
    out = _guard_out_dir(Path(args.out))
    cases = [json.loads(ln) for ln in (out / "cases.jsonl").read_text().splitlines() if ln]
    results = [json.loads(ln) for ln in (out / "results.jsonl").read_text().splitlines() if ln]
    summary = score(cases, results)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    for k, v in summary.items():
        print(f"{k:26} {v}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="каталог датасета (вне репо)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export")
    ex.add_argument("--days", type=int, default=30)
    kc = sub.add_parser("kg-context", help="дописать снимки/реконструкцию в готовый cases.jsonl")
    for p in (ex, kc):
        p.add_argument("--context", default="lastoasisgame-local")
        p.add_argument("--namespace", default="sre-ai")
        p.add_argument("--db-user", default="sre_ai")
        p.add_argument("--db-name", default="sre_copilot")
    rn = sub.add_parser("run")
    rn.add_argument("--limit", type=int, default=3, help="кейсов за заход (каждый ≈ 15 вызовов LLM)")
    rn.add_argument("--ids", default="", help="event_id через запятую — прогнать только их")
    sub.add_parser("score")
    args = ap.parse_args()
    return {"export": cmd_export, "kg-context": cmd_kg_context, "run": cmd_run,
            "score": cmd_score}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
