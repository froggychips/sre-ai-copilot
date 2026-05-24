"""Эвристика предложения owner-а для unowned-namespace.

Используется в `stats_digest.py` для секции `🔎 Unowned namespaces`:
если namespace упомянут в digest но не имеет team_owner в kg_services,
пытаемся подсказать вероятного owner-а.

Архитектура multi-signal (KG Coverage #3, 2026-05-24):

Старая prefix-only логика (PR #81) расширена до взвешенного fusion-а трёх
независимых сигналов + manual override:

  A. **Prefix** (weight 0.4) — regex-таблица по ns-имени (`squad-N-*`,
     `<env>-kingdom<N>`, bare `monitoring`/`kube-system` → `platform` и т.п.).
     Стабильный, но не покрывает «странные» ns без явного prefix-а.

  B. **Deploy history** (weight 0.4) — most-frequent `triggered_by` из
     `kg_deployments` за последние 30 дней для сервисов в этом ns. Username
     транслируется через `owner_aliases.resolve_username` → `@squad-N`.
     Покрывает кейс «ns без префикса, но один человек туда стабильно деплоит».

  C. **Labels** (weight 0.2) — k8s labels `team` / `owner` / `squad` /
     `app.kubernetes.io/part-of` из `kg_services.metadata_json` для любого
     сервиса в ns. Самый слабый сигнал (часто labels отсутствуют), но если
     есть — explicit declaration.

  **Manual override** — `OWNERSHIP_MANIFEST_PATH=ownership.yaml` со списком
  `[{ns_pattern, owner, reason}]`. Match по pattern (glob) → confidence=1.0,
  все эвристики игнорируются.

Слияние: для каждого кандидата суммируются weights × signal_strength,
top-1 побеждает; `confidence` — итоговый score, ограничен [0, 1].

Backward compat: старая `suggest_owner_for_ns(ns)` оставлена как
deprecated wrapper над A+kg-fallback (без B/C, без confidence).

Pure-functions, без I/O кроме передаваемого db Session и файла manifest.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services import owner_aliases

log = logging.getLogger(__name__)


# ── Сигнал A: prefix patterns ─────────────────────────────────────────────
# Префикс-патерны в порядке specificity (более узкие выше).
# Каждый паттерн вытаскивает «team-токен» из ns-имени.
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


# Веса сигналов. Сумма не нормализована к 1.0 намеренно — мы не делаем soft-max,
# а просто складываем; calibration в тестах подтверждает что 0.4/0.4/0.2 даёт
# адекватные confidence axes (см. test_owner_inference_multi.py).
_W_PREFIX = 0.4
_W_DEPLOY = 0.4
_W_LABELS = 0.2

# Окно для deploy-history сигнала. 30 дней — достаточно чтобы поймать
# регулярного контрибьютора, но не слишком long-tail (старые деплои уже
# stale relative to current ownership).
_DEPLOY_LOOKBACK_DAYS = 30


@dataclass
class OwnerSuggestion:
    """Результат multi-signal inference.

    Поля:
      - owner: предлагаемый team (`squad-N`, `platform`, …) или None если все
               сигналы пустые. **Без `@` префикса** — caller сам добавляет.
      - confidence: [0, 1]. 1.0 = manual override. ≥0.8 = high confidence.
                   <0.5 = ненадёжный suggest (caller рисует с `?`).
      - sources: список сработавших сигналов: 'prefix', 'deploy_history',
                 'labels', 'manual'. Для observability и debug.
      - manual: True если outcome из manual manifest (caller добавляет «(manual)»).
    """
    owner: Optional[str]
    confidence: float
    sources: List[str] = field(default_factory=list)
    manual: bool = False


# ── Сигнал A helper ──────────────────────────────────────────────────────


def _try_prefix_match(ns: str) -> Optional[str]:
    """Прогнать ns через regex-таблицу. None если ни один не сработал."""
    for pattern, transform in _PREFIX_PATTERNS:
        m = pattern.match(ns)
        if m is not None:
            return transform(m)
    return None


# ── Сигнал B helper: deploy history ──────────────────────────────────────


def _deploy_history_top(
    db: Session,
    ns: str,
    *,
    lookback_days: int = _DEPLOY_LOOKBACK_DAYS,
) -> Optional[Tuple[str, float]]:
    """Самый частый triggered_by в kg_deployments по сервисам ns за окно.

    Возвращает (team, strength) или None если данных нет.
      - team: `squad-N` / `platform` / `?-{username}` — без `@`.
      - strength: доля most-frequent от общего числа deploys в окне, [0, 1].
                  Например 5 деплоев у kemyashev из 6 общих → 0.83.

    Низкая strength (один deploy за месяц от незнакомого юзера) даст низкий
    итоговый confidence — что и хочется: «может это и не owner, а just commit».
    """
    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    try:
        rows = db.execute(text("""
            SELECT d.triggered_by, COUNT(*) AS cnt
            FROM kg_deployments d
            JOIN kg_services s ON s.id = d.service_id
            WHERE s.namespace = :ns
              AND d.triggered_by IS NOT NULL
              AND d.started_at >= :cutoff
            GROUP BY d.triggered_by
            ORDER BY cnt DESC
            LIMIT 5
        """), {"ns": ns, "cutoff": cutoff}).fetchall()
    except Exception as e:
        log.debug("deploy_history_top(%s): db error: %s", ns, e)
        return None

    if not rows:
        return None

    total = sum(int(r[1]) for r in rows)
    if total == 0:
        return None

    top_user, top_cnt = rows[0][0], int(rows[0][1])
    strength = top_cnt / total

    resolved = owner_aliases.resolve_username(top_user)
    # Strip leading `@` — caller добавляет.
    team = resolved.lstrip("@")
    return team, strength


# ── Сигнал C helper: labels ──────────────────────────────────────────────


# Ключи которые ищем в metadata_json (плоско и в "labels" sub-key).
# Порядок специфичности: более специфичные выше.
_LABEL_KEYS = ("team", "owner", "squad", "app.kubernetes.io/part-of")


def _extract_label_owner(metadata: Any) -> Optional[str]:
    """Из metadata_json (dict/JSON) попытаться достать owner-токен из labels.

    Смотрим:
      1. `metadata["labels"][key]` для key in _LABEL_KEYS — стандартное место.
      2. `metadata[key]` напрямую — на случай если sync положил labels плоско.

    None если ничего не нашли.
    """
    if not isinstance(metadata, dict):
        return None

    labels = metadata.get("labels")
    if isinstance(labels, dict):
        for k in _LABEL_KEYS:
            v = labels.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()

    for k in _LABEL_KEYS:
        v = metadata.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    return None


def _labels_top(db: Session, ns: str) -> Optional[Tuple[str, float]]:
    """Самый частый label-owner среди сервисов ns.

    Возвращает (team, strength) где strength — доля сервисов с этим label-ом.
    """
    try:
        rows = db.execute(text("""
            SELECT metadata_json
            FROM kg_services
            WHERE namespace = :ns AND metadata_json IS NOT NULL
        """), {"ns": ns}).fetchall()
    except Exception as e:
        log.debug("labels_top(%s): db error: %s", ns, e)
        return None

    if not rows:
        return None

    from collections import Counter
    counter: Counter = Counter()
    for (meta,) in rows:
        v = _extract_label_owner(meta)
        if v:
            counter[v] += 1

    if not counter:
        return None

    top_team, top_cnt = counter.most_common(1)[0]
    strength = top_cnt / len(rows)
    return top_team, strength


# ── Manual manifest ──────────────────────────────────────────────────────


@dataclass
class _ManifestRule:
    ns_pattern: str
    owner: str
    reason: str = "manual"


_MANIFEST_CACHE: Optional[List[_ManifestRule]] = None
_MANIFEST_CACHE_PATH: Optional[str] = None


def _load_manifest(path: str) -> List[_ManifestRule]:
    """Прочитать ownership.yaml — список dict с ns_pattern/owner.

    Невалидный файл → пустой list (с warning'ом).
    """
    try:
        p = Path(path)
        if not p.exists():
            return []
        with p.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        if not isinstance(raw, list):
            log.warning("ownership_manifest: %s — ожидался list, получили %s",
                        path, type(raw).__name__)
            return []
        out: List[_ManifestRule] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            pat = entry.get("ns_pattern")
            owner = entry.get("owner")
            if not isinstance(pat, str) or not isinstance(owner, str):
                continue
            reason = entry.get("reason", "manual")
            if not isinstance(reason, str):
                reason = "manual"
            out.append(_ManifestRule(ns_pattern=pat, owner=owner, reason=reason))
        return out
    except Exception as e:
        log.warning("ownership_manifest: не смог прочитать %s: %s", path, e)
        return []


def _get_manifest() -> List[_ManifestRule]:
    """Прочитать manifest из ENV `OWNERSHIP_MANIFEST_PATH` (кэшируем по пути)."""
    global _MANIFEST_CACHE, _MANIFEST_CACHE_PATH
    path = os.environ.get("OWNERSHIP_MANIFEST_PATH", "").strip()
    if not path:
        return []
    if _MANIFEST_CACHE is not None and _MANIFEST_CACHE_PATH == path:
        return _MANIFEST_CACHE
    rules = _load_manifest(path)
    _MANIFEST_CACHE = rules
    _MANIFEST_CACHE_PATH = path
    return rules


def _try_manifest_match(ns: str) -> Optional[_ManifestRule]:
    """Match ns против manifest rules (glob через fnmatch). Первый совпавший."""
    for rule in _get_manifest():
        if fnmatch.fnmatchcase(ns, rule.ns_pattern):
            return rule
    return None


def reset_manifest_cache() -> None:
    """Тестовый хелпер."""
    global _MANIFEST_CACHE, _MANIFEST_CACHE_PATH
    _MANIFEST_CACHE = None
    _MANIFEST_CACHE_PATH = None


# ── KG fallback (legacy) ─────────────────────────────────────────────────


def _try_kg_lookup(db: Session, ns: str) -> Optional[str]:
    """Спросить kg_services: есть ли в этом ns хоть один сервис с team_owner?

    Legacy-функция для backward compat с suggest_owner_for_ns.
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
        return None
    if row is None:
        return None
    return row[0]


# ── Main multi-signal API ────────────────────────────────────────────────


def suggest_owner_multi_signal(
    ns: str,
    db: Optional[Session] = None,
) -> OwnerSuggestion:
    """Главная функция multi-signal. ns → OwnerSuggestion.

    Контракт:
      - Пустой/`(no-ns)` → OwnerSuggestion(None, 0.0, []).
      - Manual manifest match → confidence=1.0, sources=['manual'].
      - Иначе — fusion трёх сигналов (см. модульный docstring).

    Никогда не бросает: при ошибках БД деградирует до доступных сигналов.
    """
    if not ns or ns == "(no-ns)":
        return OwnerSuggestion(None, 0.0, [])

    # 1. Manual override — высший приоритет.
    rule = _try_manifest_match(ns)
    if rule is not None:
        # Strip leading `@` из manifest owner-а — caller добавит сам.
        owner = rule.owner.lstrip("@")
        return OwnerSuggestion(
            owner=owner,
            confidence=1.0,
            sources=["manual"],
            manual=True,
        )

    # 2. Собираем сигналы (каждый opt-in: None если не сработал).
    candidates: Dict[str, float] = {}
    sources_used: List[str] = []

    # A: prefix (strength всегда 1.0 — либо matched, либо нет).
    a = _try_prefix_match(ns)
    if a is not None:
        candidates[a] = candidates.get(a, 0.0) + _W_PREFIX * 1.0
        sources_used.append("prefix")

    # B: deploy history. Требует db.
    if db is not None:
        b = _deploy_history_top(db, ns)
        if b is not None:
            team, strength = b
            candidates[team] = candidates.get(team, 0.0) + _W_DEPLOY * strength
            sources_used.append("deploy_history")

    # C: labels. Требует db.
    if db is not None:
        c = _labels_top(db, ns)
        if c is not None:
            team, strength = c
            candidates[team] = candidates.get(team, 0.0) + _W_LABELS * strength
            sources_used.append("labels")

    # Если ни один сигнал не сработал — нет догадки.
    if not candidates:
        return OwnerSuggestion(None, 0.0, [])

    # Top-1 кандидат.
    best_owner, best_score = max(candidates.items(), key=lambda kv: kv[1])

    # Confidence ограничен [0, 1]. Сумма weights = 1.0 → score in [0, 1].
    confidence = min(1.0, max(0.0, best_score))

    return OwnerSuggestion(
        owner=best_owner,
        confidence=confidence,
        sources=sources_used,
        manual=False,
    )


# ── Backward-compat legacy API ───────────────────────────────────────────


def suggest_owner_for_ns(ns: str, db: Optional[Session] = None) -> Optional[str]:
    """**DEPRECATED** — use `suggest_owner_multi_signal`.

    Старая prefix + KG-fallback логика (PR #81). Оставлено для не-мигрированных
    callers. Возвращает только owner string (без confidence/sources).

    Контракт (без изменений):
      - ns пустой / `(no-ns)` → None.
      - manual manifest match → owner оттуда.
      - префикс match → owner из паттерна.
      - KG match → owner из БД.
      - иначе None.
    """
    if not ns or ns == "(no-ns)":
        return None

    # Manual override и здесь должен выигрывать — иначе legacy callers будут
    # игнорировать override-ы. Это безопасное расширение (раньше manifest
    # просто не существовал).
    rule = _try_manifest_match(ns)
    if rule is not None:
        return rule.owner.lstrip("@")

    prefix_match = _try_prefix_match(ns)
    if prefix_match is not None:
        return prefix_match

    if db is not None:
        kg_match = _try_kg_lookup(db, ns)
        if kg_match is not None:
            return kg_match

    return None


def suggest_owners_bulk(
    namespaces: List[str],
    db: Optional[Session] = None,
) -> Dict[str, Optional[str]]:
    """Bulk-helper (legacy). Один SQL-roundtrip на список ns вместо N.

    **DEPRECATED-soft**: предпочесть `suggest_owner_multi_signal` per-ns
    в stats_digest. Оставлено для callers которым нужен только prefix+KG.

    Возвращает dict ns → suggested_owner (None если нет догадки).
    """
    result: Dict[str, Optional[str]] = {}
    kg_pending: List[str] = []

    for ns in namespaces:
        # Manual override берёт верх.
        rule = _try_manifest_match(ns)
        if rule is not None:
            result[ns] = rule.owner.lstrip("@")
            continue

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
        pass

    return result
