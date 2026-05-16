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
import logging
import re
from typing import List, Optional

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


@with_external_retry(
    max_attempts=3, initial_delay=0.5, name="statics.run_check",
    retry_on=(psycopg2.OperationalError, psycopg2.InterfaceError),
)
def _run_statics_check(error_text: str, recent_n: int) -> Optional[str]:
    """Sync check: подключаемся к statics, ищем таблицы по ключевым словам.

    Retry-ится только на connection-class ошибках (OperationalError/
    InterfaceError) — semantic SQL errors (broken query, missing table)
    retry-ить смысла нет."""
    keywords = _extract_keywords(error_text)
    if not keywords:
        return None

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
            conn.close()
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
            except Exception as e:
                verdicts.append(f"  {tbl}: query_failed ({e})")

        lines.extend(verdicts)

        any_empty = any("EMPTY" in v for v in verdicts)
        if any_empty:
            lines.append("VERDICT: Some statics tables are EMPTY — DI provider cannot load config")
        else:
            lines.append("VERDICT: Statics tables have data — DI issue likely in code, not statics data")

        conn.close()
        return "\n".join(lines)

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
