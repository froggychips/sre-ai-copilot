"""Statics Postgres enrichment.

По тексту ошибки извлекает ключевые слова (имена провайдеров/энтити),
ищет соответствующие таблицы в statics и проверяет что данные в них есть.

Цель: подтвердить или опровергнуть гипотезу "statics misconfiguration".

Пример: error = "Unable to resolve IStatics CityEffectListProvider"
→ keyword = "CityEffect"
→ таблица _effectList_base / effectList_base → SELECT count(*) > 0 → OK / EMPTY
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

import psycopg2
import psycopg2.extras
from psycopg2 import sql as pgsql

from app.config import settings
from app.services.resilience import with_external_retry

logger = logging.getLogger(__name__)

_PROVIDER_RE = re.compile(
    r"IStatics\s+(\w+?)(?:ListProvider|Provider|Service|Factory)?(?:\b|$)",
    re.IGNORECASE,
)
_CAMEL_SPLIT = re.compile(r"[A-Z][a-z]+")  # CamelCase split: CityEffect → ['City', 'Effect']


def _extract_keywords(error_text: str) -> List[str]:
    """Извлечь ключевые слова из текста ошибки."""
    keywords: list[str] = []
    # IStatics XxxYyyProvider → ['XxxYyy', 'Xxx', 'Yyy']
    for m in _PROVIDER_RE.finditer(error_text):
        full = m.group(1)  # e.g. CityEffect
        keywords.append(full.lower())
        # camel-split: CityEffect → ['city', 'effect']
        for part in _CAMEL_SPLIT.findall(full):
            if len(part) > 2:
                keywords.append(part.lower())
    if not keywords:
        # fallback: слова >= 5 символов из error_text
        keywords = [w.lower() for w in re.findall(r"\b[A-Za-z]{5,}\b", error_text)][:5]
    return list(dict.fromkeys(keywords))  # deduplicate, preserve order


def _conn_kwargs() -> dict:
    return {
        "host": settings.STATICS_HOST,
        "port": settings.STATICS_PORT,
        "user": settings.STATICS_USER,
        "password": settings.STATICS_PASSWORD,
        "connect_timeout": 5,
    }


# Транзиентные классы psycopg2: обрыв/недоступность коннекта к statics-PG.
# ВАЖНО: внутри декорированных функций их нельзя глотать широким
# `except Exception → return None` — with_external_retry (resilience.py)
# ретраит ТОЛЬКО на raise, а проглоченная ошибка возвращала None с первой
# попытки, и retry был мёртвым кодом. Деградация в None живёт в
# НЕдекорированных обёртках ниже — после исчерпания попыток (как в
# clickhouse_service.get_blast_radius, где try/except снаружи client.query).
_STATICS_TRANSIENT = (psycopg2.OperationalError, psycopg2.InterfaceError)


@with_external_retry(
    max_attempts=3, initial_delay=0.5, name="statics.run_check",
    retry_on=_STATICS_TRANSIENT,
)
def _statics_check_query(error_text: str, recent_n: int) -> Optional[str]:
    """Sync check: подключаемся к statics, ищем таблицы по ключевым словам.

    Retry-ится только на connection-class ошибках (OperationalError/
    InterfaceError) — semantic SQL errors (broken query, missing table)
    retry-ить смысла нет. Транзиентные ошибки НАМЕРЕННО пробрасываются
    наружу: их ловит декоратор (повтор), а затем _run_statics_check
    (деградация в None)."""
    keywords = _extract_keywords(error_text)
    if not keywords:
        return None

    conn = None
    try:
        conn = psycopg2.connect(database="gd", **_conn_kwargs())
        conn.autocommit = True
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Текущая версия gd — список последних N схем через schema_migrations
        cur.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT %s",
            (recent_n,),
        )
        migration_rows = cur.fetchall()
        migration_versions = [r["version"] for r in migration_rows]

        # Найти таблицы, имена которых содержат ключевые слова.
        # f-string собирает только повторы плейсхолдера `%s` — реальные значения
        # связываются через params-список, инъекция через keywords невозможна.
        like_clauses = " OR ".join("table_name ILIKE %s" for _ in keywords)
        sql_find_tables = f"SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND ({like_clauses}) ORDER BY table_name"  # nosec B608 — like_clauses — только повторы `%s`, значения связываются ниже
        cur.execute(sql_find_tables, [f"%{kw}%" for kw in keywords])
        matching_tables = [r["table_name"] for r in cur.fetchall()]

        lines = ["=== STATICS CHECK ==="]
        lines.append(f"Error keywords: {', '.join(keywords)}")
        if migration_versions:
            lines.append(f"Current statics migrations: latest={migration_versions[0]}, last {recent_n}: {migration_versions}")

        if not matching_tables:
            lines.append(f"⚠️  No statics tables found matching keywords {keywords}")
            lines.append("VERDICT: Statics tables missing — likely DI misconfiguration source")
            return "\n".join(lines)

        lines.append(f"Matching tables: {matching_tables}")

        # Проверяем каждую таблицу на наличие данных.
        # tbl приходит из information_schema (internal), но всё равно
        # квотим через psycopg2.sql.Identifier — корректный escape ".
        verdicts = []
        for tbl in matching_tables[:5]:
            try:
                cur.execute(
                    pgsql.SQL("SELECT count(*) AS cnt FROM {}").format(
                        pgsql.Identifier(tbl)
                    )
                )
                row = cur.fetchone()
                cnt = row["cnt"] if row else 0
                status = "OK" if cnt > 0 else "EMPTY"
                verdicts.append(f"  {tbl}: {cnt} rows → {status}")
            except _STATICS_TRANSIENT:
                # Коннект умер посреди обхода таблиц — это не «таблица битая»:
                # иначе ВСЕ оставшиеся таблицы получат query_failed и вердикт
                # уедет в шум. Пробрасываем декоратору на повтор проверки.
                raise
            except Exception as e:
                verdicts.append(f"  {tbl}: query_failed ({e})")

        lines.extend(verdicts)

        any_empty = any("EMPTY" in v for v in verdicts)
        if any_empty:
            lines.append("VERDICT: Some statics tables are EMPTY — DI provider cannot load config")
        else:
            lines.append("VERDICT: Statics tables have data — DI issue likely in code, not statics data")

        return "\n".join(lines)

    finally:
        # with_external_retry ретраит OperationalError/InterfaceError —
        # каждая неудачная попытка обязана закрыть свой коннект, иначе они
        # копятся (psycopg2-leak, Infra H6). conn=None если connect() упал.
        if conn is not None:
            conn.close()


def _run_statics_check(error_text: str, recent_n: int) -> Optional[str]:
    """Graceful-degrade обёртка над _statics_check_query.

    Ретраи живут в декораторе внутри; None здесь — уже ПОСЛЕ исчерпания
    попыток (транзиент) либо на детерминированной ошибке (битый SQL,
    отсутствующая таблица schema_migrations).
    """
    try:
        return _statics_check_query(error_text, recent_n)
    except Exception as e:
        logger.warning("statics_service.check_failed: %s", e)
        return None


async def check_statics_for_error(error_text: str) -> Optional[str]:
    """Async wrapper для _run_statics_check."""
    if not settings.STATICS_HOST or not settings.STATICS_PASSWORD:
        return None
    if not error_text:
        return None
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _run_statics_check, error_text, settings.STATICS_RECENT_VERSIONS
    )


# ── Statics version delta tracking (restart-attribution, инцидент 2026-07-02) ──
#
# В statics-Postgres КАЖДАЯ версия статики — отдельная БД вида `v<N>-<env>`
# (`v10401-prod`, `v10400-preprod`, `v10432-squad-gd`, …). Отдельной таблицы-
# реестра версий с timestamp'ом НЕТ: `schema_migrations` внутри версии хранит
# лишь (version bigint, dirty bool), а времени создания БД в каталоге не достать
# (`track_commit_timestamp` off, `pg_stat_file` запрещён роли). Поэтому:
#   1. «последняя версия env» = максимум номера среди БД `v%-<env>` в
#      `pg_database` (get_latest_statics_version).
#   2. ВРЕМЯ bump'а получаем version-delta через Redis: копайлот периодически
#      (beat `kg_statics_versions_sync`) + on-demand (в enrichment) наблюдает
#      номер версии и хранит `statics:seen:<env> = {version, prev_version,
#      first_observed_at}`. При СМЕНЕ номера обновляет first_observed_at=now —
#      это и есть момент наката (с точностью до каденса наблюдения). «Недавний
#      bump» = first_observed_at в окне до fired_at + известен prev_version.
# Работает СЕГОДНЯ без правок statics-Postgres. Каденс: без периодического
# beat'а первый инцидент после наката на «холодном» Redis не даст prev_version
# (нет «до»-снимка) → тихо дегрейдит в collateral; beat закрывает это окно.

# k8s-namespace → суффикс env в имени version-БД. prod-*/preprod-*/preupdate-*
# делят одну статику на весь env (не per-kingdom): `prod-kingdom4` и
# `prod-shared` → 'prod'. squad изолирован: `squad-12-shared` → 'squad-12',
# `squad-gd-shared` → 'squad-gd'.
_SQUAD_NS_RE = re.compile(r"^(squad-(?:gd|\d+))\b")

# Redis-ключ version-delta + TTL. TTL с большим запасом над окном наката, чтобы
# «до»-снимок пережил простой без beat'а; каждое наблюдение продлевает TTL.
_STATICS_SEEN_KEY = "statics:seen:{env}"
_STATICS_SEEN_TTL_SECONDS = 7 * 24 * 3600

_redis_client = None


def statics_env_from_namespace(namespace: Optional[str]) -> Optional[str]:
    """Отобразить k8s-namespace на суффикс env в имени version-БД статики.

    None — namespace не относится к игровым env (monitoring/kube-system/…),
    статика к нему неприменима.
    """
    ns = (namespace or "").strip()
    if not ns:
        return None
    if ns.startswith("prod"):
        return "prod"
    if ns.startswith("preprod"):
        return "preprod"
    if ns.startswith("preupdate"):
        return "preupdate"
    m = _SQUAD_NS_RE.match(ns)
    if m:
        return m.group(1)
    return None


@with_external_retry(
    max_attempts=3, initial_delay=0.5, name="statics.latest_version",
    retry_on=_STATICS_TRANSIENT,
)
def _latest_statics_version_query(env: str) -> Optional[Dict]:
    """Sync: последняя (и предыдущая по номеру) версия статики для env.

    env — суффикс имени version-БД ('prod'/'preprod'/'preupdate'/'squad-N'/
    'squad-gd'). Возвращает `{version, prev_version, datname, env}` либо None
    если версий для env нет. Retry только на connection-class ошибках, они
    пробрасываются наружу к декоратору (см. _STATICS_TRANSIENT)."""
    conn = None
    try:
        conn = psycopg2.connect(database="gd", **_conn_kwargs())
        conn.autocommit = True
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # `v%-<env>` — литеральный `%`-wildcard после `v` + суффикс env. Наши
        # env не содержат LIKE-метасимволов (`%`/`_`), инъекция невозможна:
        # значение связывается параметром.
        pattern = f"v%-{env}"
        cur.execute(
            """
            SELECT datname,
                   (substring(datname FROM '^v([0-9]+)-'))::bigint AS ver
            FROM pg_database
            WHERE datname LIKE %s
              AND substring(datname FROM '^v([0-9]+)-') IS NOT NULL
            ORDER BY ver DESC
            LIMIT 2
            """,
            (pattern,),
        )
        rows = cur.fetchall()
        if not rows:
            return None
        top = rows[0]
        version = int(top["ver"])
        prev_version = int(rows[1]["ver"]) if len(rows) > 1 else None
        return {
            "version": version,
            "prev_version": prev_version,
            "datname": top["datname"],
            "env": env,
        }
    finally:
        if conn is not None:
            conn.close()


def _run_latest_statics_version(env: str) -> Optional[Dict]:
    """Graceful-degrade обёртка над _latest_statics_version_query.

    None — после исчерпания ретраев (транзиент) либо на детерминированной
    ошибке. Вызывающий (observe_statics_version) дегрейдит в прежнее
    поведение «версия неизвестна».
    """
    try:
        return _latest_statics_version_query(env)
    except Exception as e:
        logger.warning("statics_service.latest_version_failed env=%s: %s", env, e)
        return None


def get_latest_statics_version(env: str) -> Optional[Dict]:
    """Последняя версия статики для env (sync, best-effort).

    None если statics-Postgres не сконфигурирован или версий нет.
    Возвращает dict `{version, prev_version, datname, env}`.
    """
    if not settings.STATICS_HOST or not settings.STATICS_PASSWORD:
        return None
    if not env:
        return None
    return _run_latest_statics_version(env)


def _get_redis():
    """Lazy sync Redis-клиент. None если Redis недоступен (fail-open)."""
    global _redis_client
    if _redis_client is None:
        try:
            import redis  # локальный импорт — не тянем redis в тестах, где не нужен
            _redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        except Exception as e:
            logger.warning("statics_service.redis_init_failed: %s", e)
            return None
    return _redis_client


def observe_statics_version(env: str) -> Optional[Dict]:
    """Наблюдать текущую версию статики env и обновить version-delta в Redis.

    Читает текущий номер версии (statics-Postgres) и сверяет с сохранённым
    снимком в Redis (`statics:seen:<env>`). При СМЕНЕ номера пишет новый снимок
    с `first_observed_at=now` и `prev_version`=прежний — это момент наката.
    При первом наблюдении env `prev_version=None` (нет «до»-снимка → накат ещё
    не подтверждён). Возвращает актуальный снимок `{version, prev_version,
    first_observed_at, env}` либо None (statics не настроен / нет версий /
    Redis недоступен → вызывающий дегрейдит в прежнее поведение).

    Идемпотентно: повторное наблюдение той же версии не двигает
    first_observed_at (сохраняется момент первого появления версии) — вердикт
    остаётся стабильным на всю волну рестартов.
    """
    latest = get_latest_statics_version(env)
    if not latest:
        return None
    current = latest["version"]
    client = _get_redis()
    if client is None:
        return None

    key = _STATICS_SEEN_KEY.format(env=env)
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        raw = client.get(key)
        if raw:
            try:
                stored = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                stored = None
        else:
            stored = None

        if stored and stored.get("version") == current:
            # Та же версия — снимок не трогаем (first_observed_at стабилен).
            return {
                "version": current,
                "prev_version": stored.get("prev_version"),
                "first_observed_at": stored.get("first_observed_at") or now_iso,
                "env": env,
            }

        # Смена версии (bump) либо первое наблюдение env.
        prev_version = stored.get("version") if stored else None
        snapshot = {
            "version": current,
            "prev_version": prev_version,
            "first_observed_at": now_iso,
        }
        client.set(key, json.dumps(snapshot), ex=_STATICS_SEEN_TTL_SECONDS)
        return {**snapshot, "env": env}
    except Exception as e:
        logger.warning("statics_service.observe_failed env=%s: %s", env, e)
        return None
