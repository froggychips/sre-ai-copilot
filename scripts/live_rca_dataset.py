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
  run     прогон N кейсов через модель → results.jsonl (дописывает, уже
          прогнанные пропускает — прогон можно дробить).
  score   метрики по results.jsonl → summary.json + таблица.

ДАННЫЕ — ТОЛЬКО ВНЕ РЕПО. Репозиторий публичный, а в кейсах живые namespace-ы,
имена сервисов, текст алертов и выводы медика. По умолчанию всё пишется в
`~/.cache/sre-ai-copilot/live-rca/<дата>/`; путь внутри репо скрипт отвергает.

Оценка — грубая и честная: причину медика и причину модели раскладываем по
одним и тем же классам (`CAUSE_CLASSES`, регулярки RU+EN), попадание — это
пересечение классов. Это нижняя оценка: модель могла назвать ту же причину
словами, которых нет в регулярках. На входе у модели только алерт — снимка
кластера на момент инцидента в БД нет (его начали сохранять с 1.0.19,
`analysis.source_coverage`), поэтому цифры меряют «разбор по алерту», а не
«разбор с полным контекстом».
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
    path = out / "cases.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False, default=str) + "\n")
    print(f"событий медика: {len(rows)}, после дедупа: {len(cases)} → {path}")
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
    ex.add_argument("--context", default="lastoasisgame-local")
    ex.add_argument("--namespace", default="sre-ai")
    ex.add_argument("--db-user", default="sre_ai")
    ex.add_argument("--db-name", default="sre_copilot")
    rn = sub.add_parser("run")
    rn.add_argument("--limit", type=int, default=3, help="кейсов за заход (каждый ≈ 15 вызовов LLM)")
    rn.add_argument("--ids", default="", help="event_id через запятую — прогнать только их")
    sub.add_parser("score")
    args = ap.parse_args()
    return {"export": cmd_export, "run": cmd_run, "score": cmd_score}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
