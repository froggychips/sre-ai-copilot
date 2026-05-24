"""Эвристика предложения owner-а для unowned-namespace.

Используется в `stats_digest.py` для секции `🔎 Unowned namespaces`:
если namespace упомянут в digest но не имеет team_owner в kg_services,
пытаемся подсказать вероятного owner-а на базе:

  1. **Префикс-патерны** — `prod-kingdom1` → `kingdom1`, `preprod-shared` →
     `shared`, `squad-7-shared` → `squad-7`. Захватываем известные WO-сущности
     `(kingdom|shared|payments|squad-N|data|monitoring|logging|etc.)`.

  2. **KG fallback** — если в kg_services по этому ns есть хотя бы один сервис
     с team_owner (даже `platform`) — отдаём этот owner. Это case когда основные
     сервисы в ns без owner, но один helper-deployment его имеет.

  3. **`?`** — иначе. UI должно прорисовать «нужен owner» без догадки.

Pure-functions, без I/O кроме передаваемого db Session. Тестируется на моках.
"""
from __future__ import annotations

import re
from typing import Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session


# Префикс-патерны в порядке specificity (более узкие выше).
# Каждый паттерн вытаскивает «team-токен» из ns-имени.
#
# Примеры:
#   prod-kingdom1   → kingdom1
#   preprod-shared  → shared
#   squad-7-shared  → squad-7
#   prod-data       → data
#   kube-system     → platform
_PREFIX_PATTERNS = [
    # squad-N-realm → squad-N (тимплейт WO: `squad-7-shared` принадлежит squad-7)
    (re.compile(r"^squad-(\d+)-"), lambda m: f"squad-{m.group(1)}"),
    # <env>-kingdom<N> → kingdom<N>
    (re.compile(r"^(?:prod|preprod|dev|staging|qa)-kingdom(\d+)$"), lambda m: f"kingdom{m.group(1)}"),
    # <env>-shared → shared
    (re.compile(r"^(?:prod|preprod|dev|staging|qa)-shared$"), lambda _m: "shared"),
    # <env>-payments / <env>-data / <env>-tools / т.п. — single-realm
    (re.compile(r"^(?:prod|preprod|dev|staging|qa)-(payments|data|tools|cdn|statics|logging|monitoring|search)$"),
     lambda m: m.group(1)),
    # bare team-namespaces без env-префикса: monitoring/logging/cert-manager/ingress-nginx
    (re.compile(r"^(monitoring|logging|cert-manager|ingress-nginx|kube-system|cattle-system|metallb-system|local-path-storage)$"),
     lambda _m: "platform"),
]


def _try_prefix_match(ns: str) -> Optional[str]:
    """Прогнать ns через regex-таблицу. None если ни один не сработал."""
    for pattern, transform in _PREFIX_PATTERNS:
        m = pattern.match(ns)
        if m is not None:
            return transform(m)
    return None


def _try_kg_lookup(db: Session, ns: str) -> Optional[str]:
    """Спросить kg_services: есть ли в этом ns хоть один сервис с team_owner?

    Возвращает не-`platform` team в первую очередь (через ORDER BY); если только
    platform — возвращаем `platform`. None если ns не в KG или все team_owner NULL.
    """
    try:
        row = db.execute(text("""
            SELECT team_owner
            FROM kg_services
            WHERE namespace = :ns AND team_owner IS NOT NULL
            ORDER BY CASE WHEN team_owner = 'platform' THEN 1 ELSE 0 END,
                     team_owner
            LIMIT 1
        """), {"ns": ns}).fetchone()
    except Exception:
        # БД недоступна / таблицы нет — fail silent (это suggester, не hard
        # требование). Caller получит `?` и нарисует «нужен owner».
        return None
    if row is None:
        return None
    return row[0]


def suggest_owner_for_ns(ns: str, db: Optional[Session] = None) -> Optional[str]:
    """Главная функция: ns → suggested team_owner или None.

    Контракт:
      - ns пустой / `(no-ns)` → None.
      - префикс match → owner из паттерна.
      - KG match → owner из БД.
      - иначе None (caller рендерит как `?`).
    """
    if not ns or ns == "(no-ns)":
        return None

    prefix_match = _try_prefix_match(ns)
    if prefix_match is not None:
        return prefix_match

    if db is not None:
        kg_match = _try_kg_lookup(db, ns)
        if kg_match is not None:
            return kg_match

    return None


def suggest_owners_bulk(
    namespaces: list[str],
    db: Optional[Session] = None,
) -> Dict[str, Optional[str]]:
    """Bulk-helper: один SQL-roundtrip на список ns вместо N запросов.

    Возвращает dict ns → suggested_owner (None если нет догадки).
    """
    result: Dict[str, Optional[str]] = {}
    kg_pending: list[str] = []

    for ns in namespaces:
        prefix_match = _try_prefix_match(ns)
        if prefix_match is not None:
            result[ns] = prefix_match
        else:
            kg_pending.append(ns)
            result[ns] = None

    if not kg_pending or db is None:
        return result

    try:
        rows = db.execute(text("""
            SELECT namespace, team_owner
            FROM (
                SELECT namespace, team_owner,
                       ROW_NUMBER() OVER (
                           PARTITION BY namespace
                           ORDER BY CASE WHEN team_owner = 'platform' THEN 1 ELSE 0 END,
                                    team_owner
                       ) AS rn
                FROM kg_services
                WHERE namespace = ANY(:nss) AND team_owner IS NOT NULL
            ) t
            WHERE rn = 1
        """), {"nss": kg_pending}).fetchall()
        for ns, owner in rows:
            result[ns] = owner
    except Exception:
        # См. _try_kg_lookup — fail silent.
        pass

    return result
