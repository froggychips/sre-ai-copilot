"""Владелец namespace как факт графа: один резолв для всех потребителей.

Вопрос «чей это стенд» до сих пор решали три несогласованных места: карта
TC-логин → Discord у squad-medic (15 записей; 08.09.2026 из 19 текущих
деплойеров сквадов в ней не было 9, и 4 из 6 больных сквадов пинговались
«владелец не определён»), `scripts/squad_dashboard.py` (лейбл deployed-by +
TeamCity + Jira) и скилл squad-occupancy в vibecode (assignee Jira по ветке).
Здесь — единственный резолв; результат лежит в `kg_namespaces.owner_*`, его
читают медик (эскалация), дашборд WO-11335 и MCP-тул `kg_squad_owners`.

Порядок (первый сработавший путь — ответ, `owner_source` говорит какой):

1. **manual** — `namespace_owners` в манифесте людей (PEOPLE_MANIFEST_PATH).
2. **jira_assignee** — WO-ключ из `deployed-branch` → assignee задачи в Jira
   → TC-логин через манифест (`jira_account_id`) или через профили TeamCity
   (`/app/rest/users`: совпадение e-mail, затем имени). Покрывает кейс
   «тимлид раскатал чужую ветку для проверки»: стенд того, чья задача.
3. **gd_claim** — кто нажал кнопку «Сквад-окружение: занять / освободить»
   (`GdSquadEnv`); номер стенда кнопка публикует как `RESOLVED_SQUAD` в
   resulting-properties. Явное действие «беру стенд» достовернее лейбла:
   09.09.2026 squad-8 и squad-15 были заняты одним человеком 24–25.08, а
   доска и медик показывали другого — прежнего деплойера из `deployed-by`,
   который с тех пор не переписывался (жалоба «инфа не обновляется»).
4. **deployed_by** — лейбл `deployed-by`, если это не сервисный аккаунт
   (ai-agent, aidev, cicd, …: 5 сквадов 08.09.2026 задеплоены агентом —
   человек за ними виден только через ветку и Jira).
5. **tc_triggered_by** — `triggered_by` последнего деплоя в kg_deployments
   по сервисам namespace, снова минус сервисные аккаунты.

Discord id — только из манифеста людей: он живёт в ConfigMap кластера,
связка логин ↔ человек в публичный репозиторий не попадает
(см. feedback: NAME_OVERRIDES выносится в env, не в код).

Внешние источники (Jira, TC) опрашиваются один раз за прогон и кэшируются
на прогон; их недоступность — не ошибка резолва, а понижение до следующего
пути с подсчётом в stats. Всё синхронно (httpx), задача идёт в beat раз в
час — стенды меняют владельца редко.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, cast

import httpx
import structlog
from sqlalchemy.orm import Session

from app.config import settings
from app.core.timeutil import ensure_naive
from app.knowledge_graph.contract import (
    NAMESPACE_OWNER_SOURCE_DEPLOYED_BY, NAMESPACE_OWNER_SOURCE_GD_CLAIM,
    NAMESPACE_OWNER_SOURCE_JIRA_ASSIGNEE, NAMESPACE_OWNER_SOURCE_MANUAL,
    NAMESPACE_OWNER_SOURCE_TC_TRIGGERED_BY)
from app.knowledge_graph.schema import (NS_STATE_ACTIVE, Deployment, Namespace,
                                        Service)

log = structlog.get_logger()

#: Ключ задачи в ветке: `wo-15194-...`, `WO-15194`, `schabanov-wo-15194-...`.
_JIRA_KEY_RE = re.compile(r"(?<![a-z0-9])(wo)-?(\d{3,6})(?![0-9])", re.IGNORECASE)

#: Учётки автоматики: за ними человека нет, владельцем они быть не могут.
#: Список расширяется манифестом (`service_accounts`), а не кодом.
DEFAULT_SERVICE_ACCOUNTS: frozenset = frozenset({
    "ai-agent", "aidev", "cicd", "teamcity", "teamcity-cicd", "qcerdh6w",
})

_HTTP_TIMEOUT = 8.0

#: Через сколько namespace коммитить промежуточный результат (см.
#: `sync_namespace_owners`): держать одну транзакцию на весь прогон нельзя.
_COMMIT_EVERY = 10


# ── манифест людей ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Person:
    login: str
    discord_id: Optional[str] = None
    jira_account_id: Optional[str] = None
    jira_email: Optional[str] = None
    jira_display_name: Optional[str] = None


@dataclass
class PeopleManifest:
    """Содержимое PEOPLE_MANIFEST_PATH. Пустой манифест — валидное состояние:
    резолв тогда обходится лейблами и TC, без Discord id."""

    people: Dict[str, Person] = field(default_factory=dict)
    service_accounts: Set[str] = field(default_factory=lambda: set(DEFAULT_SERVICE_ACCOUNTS))
    namespace_owners: Dict[str, str] = field(default_factory=dict)

    def is_service_account(self, login: Optional[str]) -> bool:
        return bool(login) and str(login).lower() in self.service_accounts

    def discord_for(self, login: Optional[str]) -> Optional[str]:
        if not login:
            return None
        p = self.people.get(str(login).lower())
        return p.discord_id if p else None

    def by_jira(self, account_id: Optional[str], email: Optional[str],
                display_name: Optional[str]) -> Optional[Person]:
        for p in self.people.values():
            if account_id and p.jira_account_id and p.jira_account_id == account_id:
                return p
        email_l = (email or "").strip().lower()
        if email_l:
            for p in self.people.values():
                if p.jira_email and p.jira_email.strip().lower() == email_l:
                    return p
        name_n = _norm_name(display_name)
        if name_n:
            for p in self.people.values():
                if _norm_name(p.jira_display_name) == name_n:
                    return p
        return None


def _norm_name(s: Optional[str]) -> str:
    return " ".join((s or "").split()).casefold()


def load_people_manifest(path: Optional[str]) -> PeopleManifest:
    """JSON → PeopleManifest. Нет пути / файла / битый JSON → пустой манифест
    с warning-ом: резолв не должен падать из-за конфигурации."""
    manifest = PeopleManifest()
    if not path:
        return manifest
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        log.warning("people_manifest.missing", path=path)
        return manifest
    except Exception as e:  # noqa: BLE001
        log.warning("people_manifest.invalid", path=path, error=str(e))
        return manifest
    if not isinstance(raw, dict):
        log.warning("people_manifest.invalid", path=path, error="корень не объект")
        return manifest
    for item in raw.get("people") or []:
        if not isinstance(item, dict) or not item.get("login"):
            continue
        login = str(item["login"]).strip().lower()
        manifest.people[login] = Person(
            login=login,
            discord_id=_opt_str(item.get("discord_id")),
            jira_account_id=_opt_str(item.get("jira_account_id")),
            jira_email=_opt_str(item.get("jira_email")),
            jira_display_name=_opt_str(item.get("jira_display_name")),
        )
    for sa in raw.get("service_accounts") or []:
        if sa:
            manifest.service_accounts.add(str(sa).strip().lower())
    for ns, login in (raw.get("namespace_owners") or {}).items():
        if ns and login:
            manifest.namespace_owners[str(ns)] = str(login).strip().lower()
    return manifest


def _opt_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


# ── внешние источники ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TcUser:
    login: str
    name: Optional[str] = None
    email: Optional[str] = None


def fetch_tc_users() -> Dict[str, TcUser]:
    """Профили TeamCity `/app/rest/users` → {login: TcUser}. Нет TC_URL/токена
    или ошибка → {} (путь через имена просто не сработает).

    Токен берётся из `TC_USERS_TOKEN`, и только если он пуст — из `TC_TOKEN`.
    Причина: основной токен принадлежит сервисной учётке `ai-agent`, у которой
    нет права смотреть профили (403 на проде 08.09.2026), и без отдельного
    токена сопоставление «assignee Jira → TC-логин» не работает вообще.
    """
    token = settings.TC_USERS_TOKEN or settings.TC_TOKEN
    if not settings.TC_URL or not token:
        return {}
    url = settings.TC_URL.rstrip("/") + "/app/rest/users"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            r = client.get(url, headers=headers, params={"fields": "user(username,name,email)"})
            r.raise_for_status()
            users = r.json().get("user") or []
    except httpx.HTTPStatusError as e:
        # 403 — не сеть и не опечатка в URL, а нехватка права смотреть
        # профили у владельца токена. Отдельная ветка, чтобы в логе была
        # причина, а не безликий HTTPStatusError.
        log.warning(
            "namespace_owner.tc_users_forbidden" if e.response.status_code == 403
            else "namespace_owner.tc_users_failed",
            status=e.response.status_code,
            used_dedicated_token=bool(settings.TC_USERS_TOKEN),
            hint=("токену нужно право смотреть профили пользователей; задайте "
                  "TC_USERS_TOKEN" if e.response.status_code == 403 else None),
        )
        return {}
    except Exception as e:  # noqa: BLE001
        log.warning("namespace_owner.tc_users_failed", error=type(e).__name__)
        return {}
    out: Dict[str, TcUser] = {}
    for u in users:
        login = _opt_str(u.get("username"))
        if not login:
            continue
        out[login.lower()] = TcUser(login=login.lower(), name=_opt_str(u.get("name")),
                                    email=_opt_str(u.get("email")))
    return out


#: Кнопка «Сквад-окружение: занять / освободить». Номер занятого стенда она
#: отдаёт только как `RESOLVED_SQUAD` в resulting-properties (в queued-параметрах
#: при авто-выборе он пуст), поэтому фильтровать по TARGET_SQUAD нельзя —
#: сканируем окно билдов и разбираем свойства.
GD_CLAIM_BUILDTYPE = "Wo_Backend_K8sNewCluster_GdSquadEnv"

#: Сколько последних билдов кнопки смотреть. Занятий мало (десятки за месяц),
#: окна с запасом хватает, а один запрос дешевле пер-сквадных.
_GD_CLAIM_WINDOW = 200


def gd_claims_from_builds(builds: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """`{squad: логин}` из ответа TeamCity по кнопке занятия.

    Чистая функция (тестируется без сети). Билды приходят от свежих к старым,
    поэтому первый успешный на сквад и есть последнее занятие. Незавершённые и
    неуспешные пропускаем: неудачная попытка занять стенд владельца не меняет.
    """
    out: Dict[str, str] = {}
    for b in builds:
        if b.get("status") != "SUCCESS" or b.get("state") != "finished":
            continue
        props = {p.get("name"): p.get("value")
                 for p in ((b.get("resultingProperties") or {}).get("property") or [])}
        squad = _opt_str(props.get("RESOLVED_SQUAD"))
        if not squad:
            continue
        squad = squad.strip().lower()
        if not re.fullmatch(r"squad-\d+", squad) or squad in out:
            continue
        login = _opt_str(((b.get("triggered") or {}).get("user") or {}).get("username"))
        if login:
            out[squad] = login.strip().lower()
    return out


def fetch_gd_claims() -> Dict[str, str]:
    """Кто последним занял каждый стенд кнопкой ГД → `{squad: логин}`.

    Нет TC_URL/токена или ошибка → `{}`: путь просто не сработает, резолв
    понизится до `deployed_by`, как было до этого.
    """
    token = settings.TC_TOKEN or settings.TC_USERS_TOKEN
    if not settings.TC_URL or not token:
        return {}
    url = settings.TC_URL.rstrip("/") + "/app/rest/builds"
    params = {
        "locator": f"buildType:{GD_CLAIM_BUILDTYPE},count:{_GD_CLAIM_WINDOW},branch:default:any",
        "fields": ("build(id,status,state,startDate,triggered(user(username)),"
                   "resultingProperties(property(name,value)))"),
    }
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            r = client.get(url, headers=headers, params=params)
            r.raise_for_status()
            builds = r.json().get("build") or []
    except Exception as e:  # noqa: BLE001 — недоступность TC не ошибка резолва
        log.warning("namespace_owner.gd_claims_failed", error=type(e).__name__)
        return {}
    claims = gd_claims_from_builds(builds)
    log.info("namespace_owner.gd_claims", squads=len(claims))
    return claims


def fetch_jira_assignee(key: str) -> Optional[Dict[str, Optional[str]]]:
    """Assignee задачи: {account_id, email, display_name}. Нет кред / 404 /
    без исполнителя / ошибка → None."""
    if not (settings.JIRA_BASE_URL and settings.JIRA_EMAIL and settings.JIRA_API_TOKEN):
        return None
    url = f"{settings.JIRA_BASE_URL.rstrip('/')}/rest/api/3/issue/{key}"
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT,
                          auth=(settings.JIRA_EMAIL, settings.JIRA_API_TOKEN)) as client:
            r = client.get(url, params={"fields": "assignee"},
                           headers={"Accept": "application/json"})
            if r.status_code == 404:
                return None
            r.raise_for_status()
            assignee = (r.json().get("fields") or {}).get("assignee") or None
    except Exception as e:  # noqa: BLE001
        log.warning("namespace_owner.jira_failed", key=key, error=type(e).__name__)
        raise JiraUnavailable(str(e)) from e
    if not assignee:
        return None
    return {
        "account_id": _opt_str(assignee.get("accountId")),
        "email": _opt_str(assignee.get("emailAddress")),
        "display_name": _opt_str(assignee.get("displayName")),
    }


class JiraUnavailable(RuntimeError):
    """Jira не ответила: путь jira_assignee пропущен, не «исполнителя нет»."""


def jira_key_from_branch(branch: Optional[str]) -> Optional[str]:
    if not branch:
        return None
    m = _JIRA_KEY_RE.search(branch)
    if not m:
        return None
    return f"{m.group(1).upper()}-{m.group(2)}"


# ── резолв ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OwnerResolution:
    login: Optional[str]
    source: Optional[str]
    jira_key: Optional[str] = None
    discord_id: Optional[str] = None
    #: Jira опрашивали, но не смогли (сеть/401) — потребитель видит, что
    #: ответ мог быть точнее.
    jira_unavailable: bool = False


JiraLookup = Callable[[str], Optional[Dict[str, Optional[str]]]]


def _login_for_assignee(assignee: Dict[str, Optional[str]], people: PeopleManifest,
                        tc_users: Dict[str, TcUser]) -> Optional[str]:
    p = people.by_jira(assignee.get("account_id"), assignee.get("email"),
                       assignee.get("display_name"))
    if p:
        return p.login
    email_l = (assignee.get("email") or "").strip().lower()
    if email_l:
        for u in tc_users.values():
            if u.email and u.email.strip().lower() == email_l:
                return u.login
    name_n = _norm_name(assignee.get("display_name"))
    if name_n:
        for u in tc_users.values():
            if _norm_name(u.name) == name_n:
                return u.login
    return None


def resolve_owner(
    namespace: str,
    deployed_by: Optional[str],
    deployed_branch: Optional[str],
    *,
    people: PeopleManifest,
    tc_users: Dict[str, TcUser],
    jira_lookup: Optional[JiraLookup],
    triggered_by_fallback: Optional[str] = None,
    gd_claim_login: Optional[str] = None,
) -> OwnerResolution:
    """Чистая функция: все внешние данные приходят параметрами."""
    jira_key = jira_key_from_branch(deployed_branch)

    manual = people.namespace_owners.get(namespace)
    if manual:
        return OwnerResolution(manual, NAMESPACE_OWNER_SOURCE_MANUAL, jira_key,
                               people.discord_for(manual))

    jira_unavailable = False
    if jira_key and jira_lookup is not None:
        try:
            assignee = jira_lookup(jira_key)
        except JiraUnavailable:
            assignee = None
            jira_unavailable = True
        if assignee:
            login = _login_for_assignee(assignee, people, tc_users)
            if login and not people.is_service_account(login):
                return OwnerResolution(login, NAMESPACE_OWNER_SOURCE_JIRA_ASSIGNEE, jira_key,
                                       people.discord_for(login), jira_unavailable)

    # Нажатие «занять» — заявка человека на стенд, и она свежее лейбла:
    # `deployed-by` остаётся от прошлого деплойера, пока новый владелец не
    # катал полный деплой сам. Ниже Jira намеренно (см. 1.0.13: «владелец из
    # задачи, а не из кнопки» — тимлид может занять стенд под чужую задачу).
    gd = (gd_claim_login or "").strip().lower() or None
    if gd and not people.is_service_account(gd):
        return OwnerResolution(gd, NAMESPACE_OWNER_SOURCE_GD_CLAIM, jira_key,
                               people.discord_for(gd), jira_unavailable)

    dep = (deployed_by or "").strip().lower() or None
    if dep and not people.is_service_account(dep):
        return OwnerResolution(dep, NAMESPACE_OWNER_SOURCE_DEPLOYED_BY, jira_key,
                               people.discord_for(dep), jira_unavailable)

    trig = (triggered_by_fallback or "").strip().lower() or None
    if trig and not people.is_service_account(trig):
        return OwnerResolution(trig, NAMESPACE_OWNER_SOURCE_TC_TRIGGERED_BY, jira_key,
                               people.discord_for(trig), jira_unavailable)

    return OwnerResolution(None, None, jira_key, None, jira_unavailable)


def last_triggered_by(db: Session, namespace: str,
                      service_accounts: Iterable[str]) -> Optional[str]:
    """`triggered_by` самого свежего деплоя по сервисам namespace, минус
    сервисные аккаунты. NULL/пусто пропускаем."""
    excluded = {s.lower() for s in service_accounts}
    rows = (
        db.query(Deployment.triggered_by)
        .join(Service, Service.id == Deployment.service_id)
        .filter(Service.namespace == namespace, Deployment.triggered_by.isnot(None))
        .order_by(Deployment.started_at.desc())
        .limit(50)
        .all()
    )
    for (who,) in rows:
        w = (who or "").strip().lower()
        if w and w not in excluded:
            return w
    return None


# ── активность (опционально) ─────────────────────────────────────────────────

#: Как в scripts/squad_dashboard.py: окно только внутри агрегатов, иначе
#: «молчит 8 дней» и «не логинились ни разу» неотличимы.
_CH_ACTIVITY_SQL = (
    "SELECT max(Timestamp) last FROM ExtLoginFact WHERE Autoplay=false FORMAT JSONCompact"
)


def fetch_squad_last_activity(squad: str) -> Optional[datetime]:
    """Последний неавтоплейный логин в ClickHouse сквада. Нет кред / CH молчит
    / ошибка → None (сигнал недоступен, а не «активности нет»)."""
    if not (settings.SQUAD_ACTIVITY_ENABLED and settings.SQUAD_CH_USER and settings.SQUAD_CH_PASSWORD):
        return None
    n = squad.split("-")[1] if "-" in squad else squad
    host = os.environ.get(f"CH_SQUAD{n}_HOST") or settings.SQUAD_CH_HOST_TEMPLATE.format(squad=squad)
    url = f"http://{host}:{settings.SQUAD_CH_PORT}/"
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT,
                          auth=(settings.SQUAD_CH_USER, settings.SQUAD_CH_PASSWORD)) as client:
            r = client.post(url, params={"database": settings.SQUAD_CH_DB},
                            content=_CH_ACTIVITY_SQL.encode())
            if r.status_code != 200:
                return None
            rows = r.json().get("data") or []
    except Exception as e:  # noqa: BLE001
        log.info("namespace_owner.ch_activity_failed", squad=squad, error=type(e).__name__)
        return None
    if not rows or not rows[0] or not rows[0][0]:
        return None
    try:
        dt = datetime.fromisoformat(str(rows[0][0]).replace(" ", "T"))
    except ValueError:
        return None
    if dt.year < 2000:  # epoch 1970 = логинов не было
        return None
    return ensure_naive(dt)


# ── прогон ───────────────────────────────────────────────────────────────────


def sync_namespace_owners(
    db: Session,
    *,
    now: Optional[datetime] = None,
    people: Optional[PeopleManifest] = None,
    tc_users: Optional[Dict[str, TcUser]] = None,
    jira_lookup: Optional[JiraLookup] = fetch_jira_assignee,
    activity_lookup: Optional[Callable[[str], Optional[datetime]]] = fetch_squad_last_activity,
    gd_claims: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Заполнить `kg_namespaces.owner_*` для active-namespace в scope.

    Внешние источники подменяются параметрами (тесты); по умолчанию — Jira и
    TC из settings. Jira опрашивается по ключу один раз за прогон.
    """
    now = ensure_naive(now or datetime.utcnow())
    scope = re.compile(settings.NAMESPACE_OWNER_SCOPE_REGEX)
    people = people if people is not None else load_people_manifest(settings.PEOPLE_MANIFEST_PATH)
    tc_users = tc_users if tc_users is not None else fetch_tc_users()
    # Один запрос на прогон: кнопка занятия отдаёт весь список стендов сразу.
    gd_claims = gd_claims if gd_claims is not None else fetch_gd_claims()

    jira_cache: Dict[str, Optional[Dict[str, Optional[str]]]] = {}
    jira_errors = 0

    def _jira(key: str) -> Optional[Dict[str, Optional[str]]]:
        nonlocal jira_errors
        if key in jira_cache:
            return jira_cache[key]
        if jira_lookup is None:
            return None
        try:
            val = jira_lookup(key)
        except JiraUnavailable:
            jira_errors += 1
            raise
        jira_cache[key] = val
        return val

    rows: List[Namespace] = (
        db.query(Namespace).filter(Namespace.state == NS_STATE_ACTIVE).all()
    )
    stats: Dict[str, Any] = {
        "scanned": 0, "resolved": 0, "unresolved": 0, "changed": 0,
        "by_source": {}, "jira_errors": 0, "activity_updated": 0,
        "people": len(people.people), "tc_users": len(tc_users),
        "gd_claims": len(gd_claims),
    }
    for row in rows:
        name = cast(str, row.namespace)
        if not scope.search(name):
            continue
        stats["scanned"] += 1
        fallback = last_triggered_by(db, name, people.service_accounts)
        squad_key = name.rsplit("-shared", 1)[0] if name.endswith("-shared") else name
        res = resolve_owner(
            name, cast(Optional[str], row.deployed_by), cast(Optional[str], row.deployed_branch),
            people=people, tc_users=tc_users, jira_lookup=_jira,
            triggered_by_fallback=fallback,
            gd_claim_login=gd_claims.get(squad_key),
        )
        before = (row.owner_login, row.owner_source, row.owner_jira_key, row.owner_discord_id)
        after = (res.login, res.source, res.jira_key, res.discord_id)
        if before != after:
            stats["changed"] += 1
            log.info("namespace_owner.changed", namespace=name,
                     owner=res.login, source=res.source, jira_key=res.jira_key)
        row.owner_login = res.login  # type: ignore[assignment]
        row.owner_source = res.source  # type: ignore[assignment]
        row.owner_jira_key = res.jira_key  # type: ignore[assignment]
        row.owner_discord_id = res.discord_id  # type: ignore[assignment]
        row.owner_resolved_at = now  # type: ignore[assignment]
        if res.login:
            stats["resolved"] += 1
            stats["by_source"][res.source] = stats["by_source"].get(res.source, 0) + 1
        else:
            stats["unresolved"] += 1

        if activity_lookup is not None:
            squad = name.rsplit("-shared", 1)[0] if name.endswith("-shared") else name
            last = activity_lookup(squad)
            if last is not None:
                row.last_activity_at = last  # type: ignore[assignment]
                stats["activity_updated"] += 1

        # Коммитим батчами, а не одной транзакцией на прогон. Прогон идёт
        # ~95 с на 46 стендах (внутри — запросы в Jira), и всё это время
        # открытая транзакция мешает соседям: в этот же день миграция
        # `20260908_0100` не взяла лок за `lock_timeout=15s` из-за чужой
        # транзакции возрастом 98 с. Заодно результат не теряется целиком,
        # если прогон упадёт на середине.
        if stats["scanned"] % _COMMIT_EVERY == 0:
            db.commit()

    stats["jira_errors"] = jira_errors
    db.commit()
    log.info("namespace_owner.synced", **{k: v for k, v in stats.items() if k != "by_source"},
             by_source=stats["by_source"])
    return stats
