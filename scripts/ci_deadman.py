#!/usr/bin/env python3
"""Deadman-канарейка для CI: следит за единственным self-hosted раннером.

Зачем отдельный процесс, а не GitHub Action: весь CI репозитория живёт на
одном self-hosted раннере (`jabbook-air-m3-sre-copilot`, личный Mac), потому
что аккаунт заблокирован от GitHub-hosted раннеров по биллингу. Значит любая
проверка «а жив ли CI», написанная как workflow, умирает вместе с объектом
наблюдения: раннер офлайн → джоба не стартует → алерта нет. Ровно так CI и
простоял мёртвым 27 дней (04.07–31.07.2026), причём молча.

Поэтому канарейка запускается ИЗВНЕ GitHub Actions — k8s CronJob в кластере
(`k8s/ci-deadman.yaml`), то есть на инфраструктуре, не зависящей от того же
мака. Проверяет через GitHub API:

  1. раннер онлайн (status != "online" → CI не на чем гонять);
  2. очередь не стоит — прогоны ждут, а НИЧЕГО не выполняется. Просто
     длинная очередь при работающем раннере авария не составляет: он здесь
     один, и три PR подряд штатно дают сорок минут ожидания;
  3. нет зелёных PR-ов, висящих дольше STALE_PR_DAYS (dependabot-дедлок:
     PR прошёл проверки и никем не смержен — сам по себе не алертится);
  4. master не красный (последний прогон CI Pipeline на master).

Молчит, когда всё хорошо: постит в Discord только при находках, чтобы не
приучать игнорировать канал. Ошибки самой проверки (сеть/токен) тоже идут
в Discord — молчащая канарейка бесполезна.

Токен не обязателен. Репозиторий публичный, и три проверки из четырёх ходят
в анонимный API (очередь, PR-ы, ветка). Токена требует только список
раннеров: `/actions/runners` отдаёт 401 даже на публичном репозитории.
Работать без него — осознанный размен: канарейка не видит статус раннера
напрямую, но ту же аварию ловит по очереди и по отменённым прогонам, а
взамен в кластере не лежит секрет с доступом к чужим репозиториям.
Появится read-only PAT (fine-grained, `Administration: Read`) — проверка
раннеров включится сама, менять ничего не нужно.

Анонимный лимит GitHub — 60 запросов в час на IP, поэтому число PR-ов,
у которых опрашиваются check-runs, ограничено MAX_PR_CHECKS.

Env: [GITHUB_TOKEN] (read-only; без него — ограниченный режим), GITHUB_REPO,
     DISCORD_WEBHOOK_URL, [RUNNER_NAME], [QUEUE_MINUTES=30],
     [STUCK_HOURS=2], [STALE_PR_DAYS=3], [MAX_PR_CHECKS=15], [DRY_RUN=1].
"""
import datetime
import os
import sys

import requests

GITHUB_API = os.environ.get("GITHUB_API", "https://api.github.com")
REPO = os.environ.get("GITHUB_REPO", "froggychips/sre-ai-copilot")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
RUNNER_NAME = os.environ.get("RUNNER_NAME", "")  # пусто → проверяем все раннеры репо
QUEUE_MINUTES = int(os.environ.get("QUEUE_MINUTES", "30"))
STALE_PR_DAYS = int(os.environ.get("STALE_PR_DAYS", "3"))
#: Сколько часов прогон может ждать, прежде чем это станет находкой ДАЖЕ при
#: работающем раннере. Обычная очередь на одном раннере рассасывается за
#: десятки минут; больше — значит он занят чем-то, что не заканчивается.
#:
#: Было 2 часа, стало 45 минут после инцидента 19.08.2026: pytest завис в
#: `wait_for_thread_shutdown` (фоновый поток держал сокет к kube-apiserver),
#: раннер полтора часа числился busy, очередь стояла — и канарейка молчала,
#: потому что порог ещё не вышел. Полный прогон CI здесь занимает ~11 минут,
#: так что 45 минут — это уже четырёхкратное превышение, а не «бывает».
STUCK_HOURS = float(os.environ.get("STUCK_HOURS", "0.75"))
# Анонимный лимит GitHub — 60 запросов/час на IP, а check-runs стоят по
# запросу на PR. Потолок оставляет запас остальным проверкам.
MAX_PR_CHECKS = int(os.environ.get("MAX_PR_CHECKS", "15"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "20"))

# Дефолтная ветка: имя master зашито у CI-workflow (on.push.branches), но
# репозиторий может переехать на main — берём из API, а не из константы.
DEFAULT_BRANCH = os.environ.get("DEFAULT_BRANCH", "")


def log(msg):
    print(msg, flush=True)


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def parse_ts(value):
    """ISO-8601 из GitHub API ('...Z') → aware datetime; None при мусоре."""
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


class RateLimited(RuntimeError):
    """Упёрлись в лимит GitHub — это не «CI сломан», а «мы ослепли».

    Разделено намеренно: без токена лимит 60/час на IP, и общий IP кластера
    может его выбрать. Молча посчитать это отсутствием проблем — худший
    исход для канарейки.
    """


def gh(path, params=None):
    """GET к GitHub API. Бросает наверх — обработка в main (см. run_checks)."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    # Без токена — анонимный запрос: на публичном репозитории этого хватает
    # всем проверкам, кроме списка раннеров.
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"

    r = requests.get(
        f"{GITHUB_API}{path}",
        headers=headers,
        params=params or {},
        timeout=TIMEOUT,
    )
    # 403/429 с исчерпанным остатком — именно лимит, а не запрет доступа.
    if r.status_code in (403, 429) and r.headers.get("X-RateLimit-Remaining") == "0":
        reset = r.headers.get("X-RateLimit-Reset", "?")
        raise RateLimited(
            f"лимит GitHub исчерпан (limit={r.headers.get('X-RateLimit-Limit')}, "
            f"reset={reset}){' — анонимный режим' if not TOKEN else ''}"
        )
    r.raise_for_status()
    return r.json()


# --- Проверки ------------------------------------------------------------


def check_runners():
    """Раннеры репозитория: хотя бы один online, и именно нужный, если задан.

    Единственная проверка, которой нужен токен: `/actions/runners` отдаёт 401
    и на публичном репозитории. Без токена — тихо пропускаем, а не сыпем
    находкой каждый час: ежечасная жалоба на известное ограничение приучает
    игнорировать канал, то есть ломает канарейку вернее, чем её отсутствие.
    Офлайн-раннер при этом всё равно виден — по `check_queue` и по отменённым
    прогонам в `check_default_branch_red`.
    """
    problems = []
    if not TOKEN:
        return []
    data = gh(f"/repos/{REPO}/actions/runners")
    runners = data.get("runners") or []
    if not runners:
        return ["**раннеров нет вообще** — CI не на чем гонять"]

    watched = [r for r in runners if not RUNNER_NAME or r.get("name") == RUNNER_NAME]
    if RUNNER_NAME and not watched:
        return [f"раннер `{RUNNER_NAME}` не зарегистрирован в репозитории"]

    for r in watched:
        status = r.get("status")
        if status != "online":
            problems.append(f"раннер `{r.get('name')}` в статусе `{status}`")
    return problems


def check_queue():
    """Очередь СТОИТ — то есть никто её не разбирает.

    Раннер, у которого умер процесс, а регистрация ещё жива, GitHub какое-то
    время показывает online — очередь при этом уже не движется. Поэтому она
    проверяется отдельно от статуса раннера.

    Но «в очереди есть задачи» и «очередь стоит» — разные вещи, и до
    17.08.2026 канарейка их путала. Раннер здесь ОДИН: три PR подряд, каждый
    со сборкой образа на ~10 минут, легко дают сорокаминутное ожидание —
    и это нормальная работа, а не авария. Такие срабатывания приучают
    игнорировать канал, то есть ломают канарейку вернее, чем её отсутствие.

    Отличаем по простому признаку: если что-то ВЫПОЛНЯЕТСЯ, очередь движется
    и раннер жив — молчим. Тревога, только когда прогоны ждут, а не делает
    никто. На этот случай порога по времени почти не нужно: простаивающая
    очередь при свободном раннере — уже неправильно.

    Страховка от вечного ожидания оставлена: если прогон висит дольше
    STUCK_HOURS даже при работающем раннере, что-то не так с ним самим.
    """
    problems = []
    queued = gh(
        f"/repos/{REPO}/actions/runs", {"status": "queued", "per_page": 20}
    ).get("workflow_runs") or []
    if not queued:
        return []

    running = gh(
        f"/repos/{REPO}/actions/runs", {"status": "in_progress", "per_page": 5}
    ).get("workflow_runs") or []

    cutoff = now_utc() - datetime.timedelta(minutes=QUEUE_MINUTES)
    hard_cutoff = now_utc() - datetime.timedelta(hours=STUCK_HOURS)
    waiting, stuck_hard = [], []
    for run in queued:
        created = parse_ts(run.get("created_at"))
        if not created:
            continue
        age_min = int((now_utc() - created).total_seconds() // 60)
        item = (f"[{run.get('name')} #{run.get('run_number')}]"
                f"({run.get('html_url')}) — {age_min} мин")
        if created < hard_cutoff:
            stuck_hard.append(item)
        elif created < cutoff:
            waiting.append(item)

    if stuck_hard:
        problems.append(
            f"**прогоны висят дольше {STUCK_HOURS} ч** — раннер занят, но не "
            f"этим:\n  · " + "\n  · ".join(stuck_hard[:5])
        )
    elif waiting and not running:
        # Никто не выполняется, а очередь ждёт — вот это и есть «встала».
        problems.append(
            f"**очередь стоит** — ничего не выполняется, а {len(waiting)} "
            f"прогон(ов) ждут дольше {QUEUE_MINUTES} мин:\n  · "
            + "\n  · ".join(waiting[:5])
        )
    return problems


def check_stale_green_prs():
    """Открытые PR-ы, у которых проверки зелёные, а возраст > STALE_PR_DAYS.

    Это про dependabot-дедлок: бот открывает PR, CI его красит зелёным, и он
    висит месяцами — ни один существующий сигнал об этом не говорит.
    """
    problems = []
    prs = gh(f"/repos/{REPO}/pulls", {"state": "open", "per_page": 50})
    cutoff = now_utc() - datetime.timedelta(days=STALE_PR_DAYS)
    stale = []
    checked = 0
    skipped = 0
    for pr in prs:
        if pr.get("draft"):
            continue
        created = parse_ts(pr.get("created_at"))
        if not created or created >= cutoff:
            continue
        # Каждый PR — отдельный запрос check-runs; в анонимном режиме их
        # столько же, сколько всего доступно за час.
        if checked >= MAX_PR_CHECKS:
            skipped += 1
            continue
        checked += 1
        sha = (pr.get("head") or {}).get("sha")
        if not sha:
            continue
        # combined status покрывает статусы-коммиты, check-runs — Actions.
        # Смотрим именно check-runs: у нас все проверки — GitHub Actions.
        checks = gh(f"/repos/{REPO}/commits/{sha}/check-runs", {"per_page": 50})
        runs = checks.get("check_runs") or []
        if not runs:
            continue
        conclusions = {c.get("conclusion") for c in runs if c.get("status") == "completed"}
        pending = any(c.get("status") != "completed" for c in runs)
        if pending or conclusions - {"success", "skipped", "neutral"}:
            continue
        age_days = (now_utc() - created).days
        stale.append(f"[#{pr.get('number')} {pr.get('title')}]({pr.get('html_url')}) — {age_days} дн")
    if stale:
        problems.append(
            f"**зелёные PR-ы висят** ({len(stale)} шт, > {STALE_PR_DAYS} дн):\n  · "
            + "\n  · ".join(stale[:8])
        )
    # Срез не должен выглядеть как «проверено всё и чисто».
    if skipped:
        problems.append(
            f"проверены не все PR-ы: {skipped} пропущено сверх лимита "
            f"MAX_PR_CHECKS={MAX_PR_CHECKS}"
        )
    return problems


def check_default_branch_red():
    """Последний завершённый прогон CI на дефолтной ветке — не failure.

    `cancelled` считается наравне с `failure`, и это не придирка к формальному
    статусу: офлайн-раннер именно так и выглядит снаружи — прогон висит в
    очереди, а потом GitHub его отменяет. Никто руками master не отменяет,
    поэтому отмена на дефолтной ветке — сигнал, а не шум. Для режима без
    токена это заменяет прямую проверку раннера.
    """
    branch = DEFAULT_BRANCH
    if not branch:
        branch = gh(f"/repos/{REPO}").get("default_branch") or "master"
    data = gh(
        f"/repos/{REPO}/actions/runs",
        {"branch": branch, "status": "completed", "per_page": 10},
    )
    runs = data.get("workflow_runs") or []
    # Берём последний прогон именно CI Pipeline: CodeQL и contract-check
    # красят ветку по своим причинам, у них своя история.
    ci_runs = [r for r in runs if (r.get("name") or "").startswith("CI Pipeline")]
    if not ci_runs:
        return []
    last = ci_runs[0]
    conclusion = last.get("conclusion")
    if conclusion not in ("failure", "cancelled"):
        return []

    headline = (
        f"**{branch} красный**"
        if conclusion == "failure"
        else f"**на {branch} отменён последний прогон** — типичный след офлайн-раннера"
    )
    return [
        f"{headline} — [{last.get('name')} #{last.get('run_number')}]"
        f"({last.get('html_url')}), {last.get('updated_at')}"
    ]


# Единственная проверка, которой нужен токен (`/actions/runners` → 401 даже на
# публичном репозитории). Флаг читает run_checks, чтобы пропуск был виден
# в логе, а не выглядел как успешная проверка.
check_runners.needs_token = True

CHECKS = (
    ("runners", check_runners),
    ("queue", check_queue),
    ("stale_prs", check_stale_green_prs),
    ("default_branch", check_default_branch_red),
)


def run_checks():
    """Все проверки; падение одной не отменяет остальные — она сама становится находкой."""
    problems = []
    for name, fn in CHECKS:
        # «OK» у проверки, которая не выполнялась, — ровно тот ложный зелёный,
        # ради которого канарейка и написана. Требование токена помечено на
        # самой функции, а не на её имени в наборе: имя — деталь конфигурации.
        if getattr(fn, "needs_token", False) and not TOKEN:
            log(f"  {name}: ПРОПУЩЕНО (нужен токен; офлайн ловится по очереди)")
            continue
        try:
            found = fn()
        except Exception as e:  # noqa: BLE001 — любая ошибка проверки это находка
            problems.append(f"проверка `{name}` не отработала: `{type(e).__name__}: {e}`")
            continue
        for p in found:
            problems.append(p)
        log(f"  {name}: {'OK' if not found else str(len(found)) + ' находк(и)'}")
    return problems


def post_discord(problems):
    lines = "\n".join(f"• {p}" for p in problems)
    payload = {
        "embeds": [
            {
                "title": "🩺 CI deadman: раннер/очередь/PR-ы",
                "description": (
                    f"Репозиторий `{REPO}`\n\n{lines}\n\n"
                    "_Канарейка живёт в кластере (CronJob `ci-deadman`), а не в Actions: "
                    "проверка CI не должна зависеть от того же раннера._"
                ),
                "color": 0xE74C3C,
                "timestamp": now_utc().isoformat(),
            }
        ]
    }
    if DRY_RUN or not WEBHOOK:
        log("DRY_RUN / нет webhook — сообщение не отправлено:")
        log(payload["embeds"][0]["description"])
        return
    r = requests.post(WEBHOOK, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    log("отправлено в Discord")


def main():
    mode = "с токеном" if TOKEN else "анонимно, без проверки раннеров"
    log(f"ci-deadman: {REPO} ({mode}; runner={RUNNER_NAME or 'любой'}, "
        f"queue>{QUEUE_MINUTES}м, prs>{STALE_PR_DAYS}д)")
    problems = run_checks()
    if not problems:
        log("всё чисто — молчим")
        return 0
    log(f"находок: {len(problems)}")
    post_discord(problems)
    # Находка — сигнал, и он уже ушёл в Discord. Возвращать здесь 1 значило
    # объявлять Job упавшим: k8s держал по failedJobsHistoryLimit Error-подов
    # в namespace (05.09.2026 — десять штук за 18 суток), а любое правило на
    # kube_job_status_failed видело в стороже сломанную джобу. Ненулевой
    # код — только за сбой самого опроса: исключение выше по стеку.
    return 0


if __name__ == "__main__":
    sys.exit(main())
