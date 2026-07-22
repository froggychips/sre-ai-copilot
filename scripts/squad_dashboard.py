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
STALE_DAYS = int(os.environ.get("STALE_DAYS", "14"))  # нет деплоя/активности дольше → «протух»
ACTIVE_DAYS = float(os.environ.get("ACTIVE_DAYS", "2"))  # живой логин не старше → стенд используется
SQUADS = os.environ.get("SQUADS")

# Per-squad ClickHouse (живая игровая активность). Хост по DNS сервиса в ns сквада,
# креды из env (для in-cluster CronJob — из secret). Нет кред/CH → сигнал просто пропускается.
CH_USER = os.environ.get("CH_USER")
CH_PASSWORD = os.environ.get("CH_PASSWORD")
CH_PORT = os.environ.get("CH_PORT", "8123")
CH_DB = os.environ.get("CH_DB", "WOAnalytics")
CH_HOST_TEMPLATE = os.environ.get("CH_HOST_TEMPLATE",
                                  "clickhouse.{squad}-shared.svc.cluster.local")
SQUAD_NUMS = [int(x) for x in SQUADS.split()] if SQUADS else list(range(1, 60))

# Резервирование новых дедик-нод под разработчиков (WO-12485): squad -> TC-логин.
# Зеркало services/squad-mapping.yaml (wo-k8s) — держать в синхроне вручную.
# Показывается в колонке «Reserved for»; сами сквады создаются позже (InstallSquadEnv).
RESERVED = {
    "squad-40": "kemyashev",   "squad-41": "kemyashev",
    "squad-42": "elebedev",    "squad-43": "elebedev",
    "squad-44": "apleshkov",   "squad-45": "apleshkov",
    "squad-46": "dgrin",       "squad-47": "dgrin",
    "squad-48": "ddosta",      "squad-49": "ddosta",
    "squad-50": "ncherkashin", "squad-51": "ncherkashin",
    "squad-52": "kkuzmin",     "squad-53": "kkuzmin",
    "squad-54": "egecer",      "squad-55": "egecer",
    "squad-56": "schabanov",   "squad-57": "schabanov",
    "squad-58": "tkolosov",    "squad-59": "tkolosov",
}
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
    """Оконный скан OneService -> {squad: {last_build, last_deployer}}.

    TC недоступен / 401 / таймаут — НЕ роняем весь дашборд: возвращаем {},
    колонка «Последняя сборка» покажет idle, а живость доски (владелец/задача из
    ns-лейблов, KG-здоровье, CH-активность) от TC-токена больше не зависит.
    """
    path = (f"/app/rest/builds?locator=buildType:{ONE},count:{WINDOW},branch:default:any"
            f"&fields=build(number,status,startDate,triggered(user(username)),"
            f"resultingProperties(property(name,value)))")
    try:
        builds = tc_get(path).get("build", [])
    except requests.RequestException as e:
        log(f"  oneservice: TC недоступен ({e}) → колонка сборки idle, дашборд продолжаем")
        return {}
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
        act = fetch_ch_activity(s)
        # Все сквады в пределах SQUAD_NUMS — реальные провизионированные слоты,
        # поэтому показываем и пустые: classify() даёт им «свободен» (доступная ёмкость).
        rows.append(dict(
            squad=s, age=k.get("age_days"), ns=k.get("ns"), svcs=k.get("svcs"),
            wh=(float(k["worst_health"]) if k.get("worst_health") is not None else None),
            ev24h=k.get("ev24h"), bo7=k.get("backoff7d"), ev7=k.get("evict7d"),
            un7=k.get("unhlth7d"), al=k.get("alerts"),
            owner=owner, task=lbl.get("task"), branch=lbl.get("branch"),
            reserved=RESERVED.get(s),
            lb=lb, inst=inst, act=act))
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
    # компактнее: RebuildSquadFromSource -> Rebuild, InstallSquadEnv -> Install; дата без времени
    bt = inst["buildtype"].replace("SquadFromSource", "").replace("SquadEnv", "")
    date = fmt_ts(inst["started"]).split(" ")[0]
    return esc(f'{bt} #{inst["number"]} · {inst["by"]} · {date}')


def activity_cell(act, today):
    """Живая активность: '5 чел · 3ч' / 'тихо' / '' (нет CH/данных)."""
    if act is None:
        return ""                       # нет CH / кред / ошибка — сигнал недоступен
    last = act.get("last")
    if not last:
        return loz("тихо", "Green")     # CH есть, живых логинов нет
    hours = (today - last).total_seconds() / 3600.0
    if hours < 0.5:
        ago = "только что"
    elif hours < 48:                      # до 2 суток — в часах (точнее, без вранья усечения)
        ago = f"{int(round(hours))}ч"
    else:
        ago = f"{int(round(hours / 24.0))}д"
    n = act.get("users_7d") or 0
    return f'{esc(n)} чел · {ago}'


def td(content):
    return f"<td><p>{content}</p></td>" if content else "<td></td>"


def td_hl(content, colour):
    """Ячейка с фоновой подсветкой (Confluence highlight-colour)."""
    attr = f' data-highlight-colour="{colour}"' if colour else ""
    return f"<td{attr}><p>{content}</p></td>" if content else f"<td{attr}></td>"


# базовые/idle-ветки: деплой с них = сквад никем не занят под конкретную работу
BASE_BRANCHES = {"preprod", "default", "master", "main", "develop"}
# подстроки статуса Jira, означающие, что тикет закрыт (сквад освобождается)
CLOSED_JIRA = ("done", "closed", "resolved", "выполнено", "закрыт", "готово", "отмен")


def is_busy(branch, task):
    """Заявлен занятым = есть WO-задача ИЛИ деплой с фиче-ветки (не базовой)."""
    if task:
        return True
    if not branch:
        return False
    b = branch.lower().replace("refs-heads-", "").replace("refs/heads/", "")
    return b not in BASE_BRANCHES


def _parse_dt(s):
    try:
        return datetime.datetime.strptime(s[:15], "%Y%m%dT%H%M%S")
    except Exception:
        return None


def last_activity_days(r, today):
    """Дней с последней активности деплоя (max из OneService и install/rebuild). None — не было."""
    ds = []
    for key in ("lb", "inst"):
        v = r.get(key)
        if v and v.get("started"):
            d = _parse_dt(v["started"])
            if d:
                ds.append(d)
    return (today - max(ds)).days if ds else None


def classify(r, jira_statuses, today):
    """Трёхуровневый статус: (надпись, цвет лозенга, фон ячейки Squad).

    Сигналы: ветка/задача (заявка), свежесть деплоя (TC), статус Jira-тикета,
    старый+крашащийся стенд. Порядок приоритетов снизу вверх в коде.
    """
    busy_label = is_busy(r["branch"], r["task"])
    # живая игровая активность (ExtLogin без автоплея) — самый честный сигнал использования
    act = r.get("act")
    act_days = None
    if act and act.get("last"):
        act_days = (today - act["last"]).total_seconds() / 86400.0
    recent_act = act_days is not None and act_days <= ACTIVE_DAYS
    quiet_act = act_days is not None and act_days > STALE_DAYS   # CH есть, давно тихо

    # свободен: никем не заявлен И не используется живыми логинами
    if not busy_label and not recent_act:
        return ("свободен", "Green", "#e3fcef")
    # реально используется прямо сейчас → занят (в т.ч. preprod-стенд с тестерами)
    if recent_act:
        return ("занят", "Red", "#ffebe6")
    # заявлен занятым, но свежей активности нет — проверяем заброшенность ПОЗИТИВНЫМИ признаками:
    # закрытый Jira-тикет, достоверно старый деплой, либо CH-молчание >STALE_DAYS.
    # idle=None (дыра атрибуции TC) и отсутствие CH — это «не знаю», НЕ протух.
    closed = False
    if r["task"]:
        st = (jira_statuses.get(r["task"]) or "").lower()
        closed = any(s in st for s in CLOSED_JIRA)
    idle = last_activity_days(r, today)
    stale_deploy = idle is not None and idle > STALE_DAYS
    if closed or stale_deploy or quiet_act:
        return ("протух", "Yellow", "#fffae6")
    return ("занят", "Red", "#ffebe6")


def fetch_jira_statuses(tasks):
    """Статусы WO-тикетов через Jira REST (те же Atlassian-креды, что и Confluence)."""
    base = os.environ.get("CONFLUENCE_BASE", "").rstrip("/")
    email = os.environ.get("CONFLUENCE_EMAIL")
    token = os.environ.get("CONFLUENCE_TOKEN")
    if not (base and email and token):
        return {}
    site = base[:-5] if base.endswith("/wiki") else base   # отрезаем /wiki → корень сайта
    auth = (email.strip(), token.strip())
    out = {}
    for t in sorted({x for x in tasks if x}):
        try:
            r = requests.get(f"{site}/rest/api/3/issue/{t}?fields=status", auth=auth, timeout=20)
            if r.status_code == 200:
                out[t] = (((r.json().get("fields") or {}).get("status") or {}).get("name"))
            else:
                log(f"  jira {t}: HTTP {r.status_code}")
        except Exception as e:
            log(f"  jira {t}: {e}")
    return out


def _parse_ch_ts(s):
    """CH DateTime64 '2026-06-05T08:15:37.934000' / без дробной части → datetime (naive UTC)."""
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# боты-автоплея пишут в server-side CreateUserSessionFact, но НЕ в клиентский ExtLoginFact;
# Autoplay=false отсекает автотест-клиентов → остаётся реальная живая активность тестеров
CH_ACTIVITY_SQL = (
    "SELECT uniqExactIf(DynUserId, Timestamp > now()-interval 24 hour) users_24h, "
    "uniqExact(DynUserId) users_7d, max(Timestamp) last "
    "FROM ExtLoginFact WHERE Autoplay=false AND Timestamp > now()-interval 7 day "
    "FORMAT JSONCompact"
)


def fetch_ch_activity(squad):
    """Живая игровая активность сквада из его ClickHouse.

    Возвращает:
      None                                  — нет кред / нет CH / ошибка (сигнал недоступен)
      {"users_24h":0,"users_7d":0,"last":None} — CH есть, активности нет («тихо»)
      {"users_24h":N,"users_7d":M,"last":dt}   — есть живые логины
    """
    if not (CH_USER and CH_PASSWORD):
        return None
    # резолвинг хоста как в tools-server: явный CH_SQUAD{N}_HOST (squad-1/2 = bare ns
    # clickhouse.squad-N.svc), иначе фолбэк-шаблон clickhouse.squad-N-shared.svc
    n = squad.split("-")[1] if "-" in squad else squad
    host = os.environ.get(f"CH_SQUAD{n}_HOST") or CH_HOST_TEMPLATE.format(squad=squad)
    url = f"http://{host}:{CH_PORT}/"
    try:
        r = requests.post(url, params={"database": CH_DB},
                          data=CH_ACTIVITY_SQL.encode(),
                          auth=(CH_USER, CH_PASSWORD), timeout=8)
        if r.status_code != 200:
            return None
        rows = r.json().get("data") or []
        if not rows:
            return {"users_24h": 0, "users_7d": 0, "last": None}
        u24, u7, last = rows[0]
        dt = _parse_ch_ts(last)
        if dt is None or dt.year < 2000:   # epoch 1970 = логинов не было
            return {"users_24h": 0, "users_7d": 0, "last": None}
        return {"users_24h": int(u24), "users_7d": int(u7), "last": dt}
    except Exception as e:
        log(f"  ch {squad}: {e}")
        return None


def render(rows, gen_date, jira_statuses=None, today=None):
    jira_statuses = jira_statuses or {}
    today = today or datetime.datetime.utcnow()
    # (заголовок, ширина px). Цифровые колонки — узкие, текстовые — широкие.
    cols = [
        ("Статус", 78), ("Squad", 78), ("Занявший", 100), ("Reserved for", 96), ("Задача", 110),
        ("Ветка", 140), ("Активность", 110), ("Последняя сборка (OneService)", 240),
        ("Установка / Rebuild", 165), ("Возраст, дн", 68), ("NS", 45),
        ("Svc", 50), ("Health", 62), ("Краши 7д", 145), ("Ev 24ч", 60),
        ("Alerts", 56),
    ]
    colgroup = ("<colgroup>"
                + "".join(f'<col style="width: {w}.0px;" />' for _, w in cols)
                + "</colgroup>")
    head = "<tr>" + "".join(f"<th><p>{h}</p></th>" for h, _ in cols) + "</tr>"
    trs = []
    jira = "https://juicybuttons.atlassian.net/browse/"
    for r in rows:
        status_txt, status_col, sq_bg = classify(r, jira_statuses, today)
        status = loz(status_txt, status_col)
        task = f'<a href="{jira}{esc(r["task"])}">{esc(r["task"])}</a>' if r["task"] else ""
        owner = esc(r["owner"]) if r["owner"] else loz("нет лейбла")
        reserved = loz(esc(r["reserved"]), "Blue") if r.get("reserved") else ""
        age = esc(r["age"]) if r["age"] is not None else loz("нет в KG")
        alerts = loz(str(r["al"]), "Red") if r["al"] else (esc(r["al"]) if r["al"] is not None else "")
        ev24 = (loz(str(r["ev24h"]), "Yellow") if (r["ev24h"] and r["ev24h"] > 20)
                else (esc(r["ev24h"]) if r["ev24h"] is not None else ""))
        trs.append(
            "<tr>"
            + td(status)
            + td_hl(f'<strong>{esc(r["squad"])}</strong>', sq_bg)
            + td(owner) + td(reserved) + td(task) + td(esc(r["branch"]))
            + td(activity_cell(r.get("act"), today))
            + td(build_cell(r["lb"])) + td(inst_cell(r["inst"])) + td(str(age))
            + td(esc(r["ns"]) if r["ns"] is not None else "")
            + td(esc(r["svcs"]) if r["svcs"] is not None else "")
            + td(health_cell(r["wh"])) + td(crash_cell(r["bo7"], r["un7"], r["ev7"]))
            + td(str(ev24)) + td(str(alerts))
            + "</tr>"
        )
    table = (f'<table data-layout="full-width">{colgroup}'
             f'<tbody>{head}{"".join(trs)}</tbody></table>')
    body = (
        f'<ac:structured-macro ac:name="info"><ac:rich-text-body>'
        f'<p><strong>Дашборд по сквадам (dev-стенды).</strong> Кто чем занял каждый squad, над какой задачей, '
        f'последняя сборка и здоровье окружения. <strong>Авто-обновление раз в 5 минут</strong> (k8s CronJob, ns sre-ai). '
        f'Снимок: {esc(gen_date)} UTC.</p></ac:rich-text-body></ac:structured-macro>'
        f'<p><strong>Источники</strong> (джойн по ключу <code>squad-N</code>): Knowledge Graph (kg_query) — возраст, NS/svc, '
        f'health, pod_events, alerts; TeamCity REST + лейблы namespace — занявший (deployed-by), задача '
        f'(deployed-branch → WO-тикет), последняя сборка (OneServiceBuildAndUpdate), провенанс install/rebuild.</p>'
        f'{table}'
        f'<h2>Как читать — расшифровка колонок</h2><ul>'
        f'<li><strong>Статус</strong> — '
        f'{loz("свободен", "Green")} не заявлен (базовая ветка / нет лейбла) И нет живых логинов — можно занимать; '
        f'{loz("занят", "Red")} заявлен (WO-задача / фиче-ветка) ИЛИ есть живая активность ≤{int(ACTIVE_DAYS)}д '
        f'(в т.ч. preprod-стенд, который реально тестируют); '
        f'{loz("протух", "Yellow")} заявлен, но без свежей активности И похож на брошенный: тикет закрыт (Done/Closed), '
        f'либо известный деплой &gt;{STALE_DAYS}д назад, либо живых логинов нет &gt;{STALE_DAYS}д — кандидат на возврат. '
        f'(Нет данных о деплое/активности — «не знаю», оставляем «занят».) '
        f'Ячейка <strong>Squad</strong> подсвечена тем же цветом.</li>'
        f'<li><strong>Занявший</strong> — кто задеплоил (лейбл namespace <code>deployed-by</code> = TC-логин).</li>'
        f'<li><strong>Reserved for</strong> — за кем закреплён сквад на выделенной 128GB-ноде (WO-12485). '
        f'Резерв (squad→разработчик) — из карты в генераторе, зеркало <code>services/squad-mapping.yaml</code> (wo-k8s). '
        f'Пусто = сквад не зарезервирован под дедик-ноду. Для squad-40..53 сквад может ещё не быть создан.</li>'
        f'<li><strong>Задача</strong> — WO-тикет из ветки деплоя; пусто при preprod/default.</li>'
        f'<li><strong>Ветка</strong> — <code>deployed-branch</code> из лейбла namespace.</li>'
        f'<li><strong>Активность</strong> — живые игровые логины из ClickHouse сквада '
        f'(<code>ExtLoginFact</code>, боты-автоплея отфильтрованы): «<em>N чел · Xч/д</em>» = уникальных '
        f'тестеров за 7д и давность последнего логина; «тихо» = CH есть, логинов нет; пусто = нет CH/данных. '
        f'Самый честный сигнал реального использования стенда.</li>'
        f'<li><strong>Последняя сборка (OneService)</strong> — последний '
        f'OneServiceBuildAndUpdate в окне; idle = сборок в окне не было.</li>'
        f'<li><strong>Установка / Rebuild</strong> — последний Install/Rebuild стенда '
        f'(TC-билд #, кто, дата).</li>'
        f'<li><strong>Возраст, дн</strong> — дней с создания первого сервиса сквада '
        f'(обрезано baseline KG, floor 2026-05-15).</li>'
        f'<li><strong>NS</strong> — число namespace’ов сквада (shared + kingdom).</li>'
        f'<li><strong>Svc</strong> — число сервисов сквада в Knowledge Graph.</li>'
        f'<li><strong>Health</strong> — worst health_score (дискретный; триаж лучше по «Краши 7д»).</li>'
        f'<li><strong>Краши 7д</strong> — pod_events за 7д: BackOff (CrashLoop) / Unhealthy (фейл проб) / Evicted. «чисто» = пусто.</li>'
        f'<li><strong>Ev 24ч</strong> — всего pod_events за последние 24 часа.</li>'
        f'<li><strong>Alerts</strong> — открытые (неразрешённые) алерты по сервисам сквада.</li></ul>'
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
    now = datetime.datetime.utcnow()
    gen_date = now.strftime("%Y-%m-%d %H:%M")
    rows = build_rows()
    jira_statuses = fetch_jira_statuses([r["task"] for r in rows])
    body = render(rows, gen_date, jira_statuses, now)
    n_owner = sum(1 for r in rows if r["owner"])
    n_lb = sum(1 for r in rows if r["lb"])
    n_task = sum(1 for r in rows if r["task"])
    n_stat = {}
    for r in rows:
        s = classify(r, jira_statuses, now)[0]
        n_stat[s] = n_stat.get(s, 0) + 1
    log(f"rows={len(rows)} owner={n_owner} last_build={n_lb} task={n_task} "
        f"status={n_stat} body_bytes={len(body)}")
    if DRY_RUN:
        log("DRY_RUN=1 → Confluence не трогаем.")
        return
    ver = publish(body, gen_date)
    log(f"published page {os.environ['CONFLUENCE_PAGE_ID']} version={ver}")


if __name__ == "__main__":
    main()
