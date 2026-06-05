#!/usr/bin/env python3
"""WO-11335 — авто-генерация Confluence-дашборда по сквадам.

Сводит два источника по ключу squad-N и перезаписывает страницу Confluence:
  * Knowledge Graph (PG, DATABASE_URL)  -> возраст, NS/svc, health, краши (pod_events), alerts
  * TeamCity REST (TC_URL/TC_TOKEN)      -> последняя сборка (OneService), провенанс install/rebuild
  * k8s ns-лейблы (in-cluster API)       -> занявший (deployed-by), задача (deployed-branch -> WO-тикет)

Запуск: k8s CronJob на образе sre-ai-copilot, SA sre-ai (умеет list namespaces).
Env: DATABASE_URL, TC_URL, TC_TOKEN, CONFLUENCE_BASE, CONFLUENCE_EMAIL, CONFLUENCE_TOKEN,
     CONFLUENCE_PAGE_ID, CONFLUENCE_TITLE, [WINDOW=400], [SQUADS="1..24"], [DRY_RUN=1].
DRY_RUN=1 — всё собрать и отрендерить, но НЕ писать в Confluence (печатает сводку).

Источники логики: ref_kg_squad_dashboard_query (KG-SQL), ~/tc_squad_last_build.sh (TC REST).
"""
import os
import re
import sys
import json
import html
import datetime
import requests
import psycopg2

WINDOW = int(os.environ.get("WINDOW", "400"))
SQUADS = os.environ.get("SQUADS")
SQUAD_NUMS = [int(x) for x in SQUADS.split()] if SQUADS else list(range(1, 25))
DRY_RUN = os.environ.get("DRY_RUN", "") not in ("", "0", "false", "False")

TC_URL = os.environ["TC_URL"].rstrip("/")
TC_TOKEN = os.environ["TC_TOKEN"].strip()  # --from-file может тащить хвостовой \n
ONE = "Wo_Backend_K8sNewCluster_OneServiceBuildAndUpdate"
INSTALL = ["Wo_Backend_K8sNewCluster_InstallSquadEnv",
           "Wo_Backend_K8sNewCluster_RebuildSquadFromSource"]

KG_SQL = """
WITH squad_svc AS (
  SELECT id, namespace, health_score, created_at,
         substring(namespace from '^(squad-[0-9]+)') AS squad
  FROM kg_services
  WHERE synthetic=false AND namespace ~ '^squad-[0-9]+'
),
agg AS (
  SELECT squad, count(*) svcs, count(DISTINCT namespace) ns,
         (now()::date - min(created_at)::date) AS age_days,
         min(health_score) AS worst_health
  FROM squad_svc GROUP BY squad
),
crashes AS (
  SELECT substring(namespace from '^(squad-[0-9]+)') AS squad,
         count(*) FILTER (WHERE last_seen > now()-interval '24 hours') AS ev_24h,
         count(*) FILTER (WHERE reason='BackOff'   AND last_seen > now()-interval '7 days') AS backoff_7d,
         count(*) FILTER (WHERE reason='Evicted'   AND last_seen > now()-interval '7 days') AS evicted_7d,
         count(*) FILTER (WHERE reason='Unhealthy' AND last_seen > now()-interval '7 days') AS unhealthy_7d
  FROM kg_pod_events WHERE namespace ~ '^squad-[0-9]+' GROUP BY 1
),
alerts AS (
  SELECT sq.squad, count(*) AS open_alerts
  FROM kg_alerts a JOIN squad_svc sq ON sq.id=a.service_id
  WHERE a.resolved_at IS NULL GROUP BY sq.squad
)
SELECT a.squad, a.age_days, a.ns, a.svcs, a.worst_health,
       coalesce(c.ev_24h,0) ev24h, coalesce(c.backoff_7d,0) backoff7d,
       coalesce(c.evicted_7d,0) evict7d, coalesce(c.unhealthy_7d,0) unhlth7d,
       coalesce(al.open_alerts,0) alerts
FROM agg a LEFT JOIN crashes c USING (squad) LEFT JOIN alerts al USING (squad)
ORDER BY a.age_days DESC;
"""


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------- 1. KG ----------
def fetch_kg():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        cur = conn.cursor()
        cur.execute(KG_SQL)
        cols = [d[0] for d in cur.description]
        rows = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}
    finally:
        conn.close()
    return rows


# ---------- 2. ns labels (in-cluster k8s API; fallback local kubectl) ----------
def fetch_ns_labels():
    out = {}
    sa = "/var/run/secrets/kubernetes.io/serviceaccount"
    if os.path.exists(f"{sa}/token"):
        token = open(f"{sa}/token").read().strip()
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        url = f"https://{host}:{port}/api/v1/namespaces"
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                         verify=f"{sa}/ca.crt", timeout=30)
        r.raise_for_status()
        items = r.json().get("items", [])
    else:  # local dev fallback
        import subprocess
        items = json.loads(subprocess.check_output(["kubectl", "get", "ns", "-o", "json"]))["items"]
    for it in items:
        name = it["metadata"]["name"]
        m = re.match(r"^(squad-[0-9]+)-shared$", name)
        if not m:
            continue
        labels = it["metadata"].get("labels", {}) or {}
        branch = labels.get("deployed-branch")
        task = None
        if branch:
            tm = re.search(r"WO-([0-9]+)", branch.upper())
            if tm:
                task = f"WO-{tm.group(1)}"
        out[m.group(1)] = {"owner": labels.get("deployed-by"), "branch": branch, "task": task}
    return out


# ---------- 3. TeamCity ----------
def tc_get(path):
    r = requests.get(TC_URL + path,
                     headers={"Authorization": f"Bearer {TC_TOKEN}", "Accept": "application/json"},
                     timeout=60)
    r.raise_for_status()
    return r.json()


def fetch_oneservice():
    """Оконный скан OneService -> {squad: {last_build, last_deployer}}."""
    path = (f"/app/rest/builds?locator=buildType:{ONE},count:{WINDOW},branch:default:any"
            f"&fields=build(number,status,startDate,triggered(user(username)),"
            f"resultingProperties(property(name,value)))")
    builds = tc_get(path).get("build", [])
    per = {}
    for b in builds:
        props = {p["name"]: p.get("value") for p in
                 (b.get("resultingProperties", {}) or {}).get("property", [])}
        ns = props.get("NAMESPACE")
        if not ns:
            continue
        sm = re.search(r"squad-[0-9]+", ns)
        if not sm:
            continue
        squad = sm.group(0)
        rec = {"number": b.get("number"), "status": b.get("status"),
               "started": b.get("startDate"),
               "by": (b.get("triggered", {}).get("user", {}) or {}).get("username", "?"),
               "service": props.get("SERVICE_NAME")}
        per.setdefault(squad, []).append(rec)
    result = {}
    for squad, rows in per.items():
        rows.sort(key=lambda r: r["started"] or "")
        last = rows[-1]
        cnt = {}
        for r in rows:
            cnt[r["by"]] = cnt.get(r["by"], 0) + 1
        top = max(cnt.items(), key=lambda kv: kv[1])
        result[squad] = {"last_build": last,
                         "last_deployer": {"by": top[0], "deploys": top[1]}}
    return result


def fetch_install(squad):
    """Последний install/rebuild по точному локатору TARGET_SQUAD."""
    found = []
    for bt in INSTALL:
        path = (f"/app/rest/builds?locator=buildType:{bt},"
                f"property:(name:TARGET_SQUAD,value:{squad},matchType:equals),count:1"
                f"&fields=build(number,status,startDate,triggered(user(username)))")
        try:
            for b in tc_get(path).get("build", []):
                found.append({"buildtype": bt.replace("Wo_Backend_K8sNewCluster_", ""),
                              "number": b.get("number"), "status": b.get("status"),
                              "started": b.get("startDate"),
                              "by": (b.get("triggered", {}).get("user", {}) or {}).get("username", "?")})
        except requests.HTTPError as e:
            log(f"  install {squad}/{bt}: {e}")
    if not found:
        return None
    found.sort(key=lambda r: r["started"] or "")
    return found[-1]


# ---------- merge ----------
def fmt_ts(s):
    if not s:
        return ""
    try:
        return datetime.datetime.strptime(s[:15], "%Y%m%dT%H%M%S").strftime("%Y-%m-%d %H:%M")
    except Exception:
        return s


def build_rows():
    kg = fetch_kg()
    labels = fetch_ns_labels()
    one = fetch_oneservice()
    rows = []
    for n in SQUAD_NUMS:
        s = f"squad-{n}"
        k = kg.get(s, {})
        lbl = labels.get(s, {})
        ov = one.get(s, {})
        lb = ov.get("last_build")
        inst = fetch_install(s)
        owner = lbl.get("owner")
        # пропускаем сквады, по которым нет вообще ничего
        if not (owner or lb or inst or k):
            continue
        rows.append(dict(
            squad=s, age=k.get("age_days"), ns=k.get("ns"), svcs=k.get("svcs"),
            wh=(float(k["worst_health"]) if k.get("worst_health") is not None else None),
            ev24h=k.get("ev24h"), bo7=k.get("backoff7d"), ev7=k.get("evict7d"),
            un7=k.get("unhlth7d"), al=k.get("alerts"),
            owner=owner, task=lbl.get("task"), branch=lbl.get("branch"),
            lb=lb, inst=inst))
    return rows


# ---------- render (Confluence storage XHTML) ----------
def esc(x):
    return html.escape("" if x is None else str(x))


def loz(text, colour=None):
    c = f'<ac:parameter ac:name="colour">{colour}</ac:parameter>' if colour else ""
    return (f'<ac:structured-macro ac:name="status"><ac:parameter ac:name="title">'
            f'{esc(text)}</ac:parameter>{c}</ac:structured-macro>')


def health_cell(wh):
    if wh is None:
        return ""
    if wh >= 1.0:
        return loz("1.0", "Green")
    if wh >= 0.7:
        return loz(str(wh), "Yellow")
    return loz(str(wh), "Red")


def crash_cell(bo, un, ev7):
    parts = []
    if bo:
        parts.append(f"BackOff {bo}")
    if un:
        parts.append(f"Unhealthy {un}")
    if ev7:
        parts.append(f"Evicted {ev7}")
    if not parts:
        return loz("чисто", "Green")
    colour = "Red" if (un and un > 50) or (bo and bo > 50) else "Yellow"
    return loz(" / ".join(parts), colour)


def build_cell(lb):
    if not lb:
        return loz("idle (нет OneService в окне)")
    st = "Green" if lb.get("status") == "SUCCESS" else "Red"
    txt = f'#{esc(lb.get("number"))} {esc(lb.get("service") or "")} — {esc(lb.get("by"))} — {esc(fmt_ts(lb.get("started")))}'
    return f'{txt} {loz(lb.get("status"), st)}'


def inst_cell(inst):
    if not inst:
        return ""
    return esc(f'{inst["buildtype"]} #{inst["number"]} ({inst["by"]}, {fmt_ts(inst["started"])})')


def td(content):
    return f"<td><p>{content}</p></td>" if content else "<td></td>"


def render(rows, gen_date):
    hdr = ["Squad", "Занявший", "Задача", "Ветка", "Последняя сборка (OneService)",
           "Установка / Rebuild", "Возр., дн", "NS", "Svc", "Health",
           "Краши 7д", "Ev 24ч", "Alerts"]
    head = "<tr>" + "".join(f"<th><p>{h}</p></th>" for h in hdr) + "</tr>"
    trs = []
    jira = "https://juicybuttons.atlassian.net/browse/"
    for r in rows:
        task = f'<a href="{jira}{esc(r["task"])}">{esc(r["task"])}</a>' if r["task"] else ""
        owner = esc(r["owner"]) if r["owner"] else loz("нет лейбла")
        age = esc(r["age"]) if r["age"] is not None else loz("нет в KG")
        alerts = loz(str(r["al"]), "Red") if r["al"] else (esc(r["al"]) if r["al"] is not None else "")
        ev24 = (loz(str(r["ev24h"]), "Yellow") if (r["ev24h"] and r["ev24h"] > 20)
                else (esc(r["ev24h"]) if r["ev24h"] is not None else ""))
        cells = [
            f'<strong>{esc(r["squad"])}</strong>', owner, task, esc(r["branch"]),
            build_cell(r["lb"]), inst_cell(r["inst"]), str(age),
            esc(r["ns"]) if r["ns"] is not None else "",
            esc(r["svcs"]) if r["svcs"] is not None else "",
            health_cell(r["wh"]), crash_cell(r["bo7"], r["un7"], r["ev7"]),
            str(ev24), str(alerts),
        ]
        trs.append("<tr>" + "".join(td(c) for c in cells) + "</tr>")
    table = (f'<table data-layout="full-width"><tbody>{head}{"".join(trs)}</tbody></table>')
    body = (
        f'<ac:structured-macro ac:name="info"><ac:rich-text-body>'
        f'<p><strong>Дашборд по сквадам (dev-стенды).</strong> Кто чем занял каждый squad, над какой задачей, '
        f'последняя сборка и здоровье окружения. <strong>Авто-обновление раз в час</strong> (k8s CronJob, ns sre-ai). '
        f'Снимок: {esc(gen_date)} UTC.</p></ac:rich-text-body></ac:structured-macro>'
        f'<p><strong>Источники</strong> (джойн по ключу <code>squad-N</code>): Knowledge Graph (kg_query) — возраст, NS/svc, '
        f'health, pod_events, alerts; TeamCity REST + лейблы namespace — занявший (deployed-by), задача '
        f'(deployed-branch → WO-тикет), последняя сборка (OneServiceBuildAndUpdate), провенанс install/rebuild.</p>'
        f'{table}'
        f'<h2>Как читать</h2><ul>'
        f'<li><strong>Health</strong> — worst health_score (дискретный; триаж лучше по «Краши 7д»).</li>'
        f'<li><strong>Краши 7д</strong> — pod_events за 7д: BackOff (CrashLoop) / Unhealthy (фейл проб) / Evicted. «чисто» = пусто.</li>'
        f'<li><strong>Последняя сборка</strong> — OneServiceBuildAndUpdate; idle = в окне сборок не было.</li>'
        f'<li><strong>Задача</strong> — WO-тикет из ветки деплоя; пусто при preprod/default.</li></ul>'
        f'<ac:structured-macro ac:name="note"><ac:rich-text-body>'
        f'<p><strong>Пробелы данных:</strong> squad без лейблов ns → занявший/задача пусты; возраст обрезан baseline KG '
        f'(floor 2026-05-15); свежие/частичные стенды — мало сервисов; last build тянется из TeamCity (deploy→squad '
        f'attribution в KG сломана: squad — runtime-параметр TC-билда).</p></ac:rich-text-body></ac:structured-macro>'
        f'<ac:structured-macro ac:name="tip"><ac:rich-text-body>'
        f'<p>Страница генерируется автоматически. Ручные правки будут перезаписаны следующим прогоном. См. WO-11335.</p>'
        f'</ac:rich-text-body></ac:structured-macro>'
    )
    return body


# ---------- Confluence ----------
def publish(body, gen_date):
    base = os.environ["CONFLUENCE_BASE"].rstrip("/")
    page_id = os.environ["CONFLUENCE_PAGE_ID"]
    title = os.environ.get("CONFLUENCE_TITLE",
                           "Дашборд по сквадам — статус, занявший, задача, сборка (WO-11335)")
    auth = (os.environ["CONFLUENCE_EMAIL"].strip(), os.environ["CONFLUENCE_TOKEN"].strip())
    cur = requests.get(f"{base}/api/v2/pages/{page_id}", auth=auth, timeout=30)
    cur.raise_for_status()
    ver = cur.json()["version"]["number"]
    payload = {"id": page_id, "status": "current", "title": title,
               "body": {"representation": "storage", "value": body},
               "version": {"number": ver + 1, "message": f"auto-refresh {gen_date} UTC"}}
    r = requests.put(f"{base}/api/v2/pages/{page_id}", auth=auth, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["version"]["number"]


def main():
    gen_date = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    rows = build_rows()
    body = render(rows, gen_date)
    n_owner = sum(1 for r in rows if r["owner"])
    n_lb = sum(1 for r in rows if r["lb"])
    n_task = sum(1 for r in rows if r["task"])
    log(f"rows={len(rows)} owner={n_owner} last_build={n_lb} task={n_task} body_bytes={len(body)}")
    if DRY_RUN:
        log("DRY_RUN=1 → Confluence не трогаем.")
        return
    ver = publish(body, gen_date)
    log(f"published page {os.environ['CONFLUENCE_PAGE_ID']} version={ver}")


if __name__ == "__main__":
    main()
