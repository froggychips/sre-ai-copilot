"""Регрессия: namespace, где владелец только `platform`, не должен быть unowned.

`_get_ns_to_team_map` держал `team_owner != 'platform'` в WHERE, а не в
FILTER. Приоритет business-team над `platform` это давало, но ценой того, что
namespace, у которого ВСЕ сервисы платформенные, выпадал из карты целиком —
и навсегда оседал в секции «🔎 Unowned namespaces — нужны owner».

Снимок 06.08.2026: так терялось 10 namespace-ов, из них с горящими сериями
`kube-system` (12 svc), `sre-ai` (4), `metallb-system` (1).

Отдельно важно, ПОЧЕМУ это не лечилось манифестом: у `sre-ai` правило
`@platform` в `config/ownership.yaml` лежало с самого начала, а namespace всё
равно числился unowned — фильтр выкидывал его до того, как манифест вообще
спрашивали. Манифест влияет на per-service inference (и на @mention в
роутинге алертов), но не на эту карту.

Логика живёт в SQL, поэтому проверяем и текст запроса (как в
test_stats_digest_fixes.py для M2), и поведение маппинга на моке.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import yaml

from app.services import stats_digest

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _captured_sql(db: MagicMock) -> str:
    """Текст SQL, с которым позвали db.execute."""
    assert db.execute.call_args is not None, "db.execute не вызывался"
    return str(db.execute.call_args[0][0])


# ── SQL: platform больше не отсекается на уровне WHERE ──────────────────────

def test_platform_only_namespace_is_not_filtered_out_in_where():
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []
    stats_digest._get_ns_to_team_map(db)

    sql = _captured_sql(db)
    normalized = " ".join(sql.split())
    assert "WHERE team_owner IS NOT NULL AND team_owner != 'platform'" not in normalized, (
        "platform всё ещё отсекается в WHERE — platform-only ns снова станут unowned"
    )
    assert "FILTER (WHERE team_owner != 'platform')" in normalized, (
        "приоритет business-team должен остаться, но через FILTER"
    )
    assert "COALESCE" in normalized, "нужен fallback на platform"


# ── Поведение маппинга ──────────────────────────────────────────────────────

def test_platform_only_namespace_gets_platform_owner():
    """kube-system / sre-ai / metallb-system → platform, а не «нет owner»."""
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [
        ("kube-system", "platform"),
        ("sre-ai", "platform"),
        ("metallb-system", "platform"),
    ]
    mapping = stats_digest._get_ns_to_team_map(db)

    assert mapping == {
        "kube-system": "platform",
        "sre-ai": "platform",
        "metallb-system": "platform",
    }


def test_business_team_still_wins_over_platform():
    """Смешанный ns отдаёт business-team — прежний приоритет не сломан.

    SQL считает это через FILTER, тут фиксируем контракт на уровне карты:
    строка, которую отдаёт запрос для смешанного ns, — business-team.
    """
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [
        ("squad-8-kingdom2", "external"),
        ("prod-kingdom2", "kingdom2"),
    ]
    mapping = stats_digest._get_ns_to_team_map(db)

    assert mapping["squad-8-kingdom2"] == "external"
    assert mapping["prod-kingdom2"] == "kingdom2"
    assert "platform" not in mapping.values()


def test_unowned_section_no_longer_receives_platform_only_ns():
    """Сквозная проверка: с owner в карте ns уходит в team, а не в unowned."""
    fired = [
        {"metric": {"namespace": "kube-system", "alertname": "TargetDown"}}
    ] * 23
    text, _unique, team_alerts, unowned = stats_digest.firing_alerts_section(
        fired, {"kube-system": "platform"}
    )

    assert team_alerts["platform"] == 23
    assert dict(unowned) == {}, f"kube-system не должен быть unowned: {dict(unowned)}"
    assert stats_digest.unowned_namespaces_section(unowned, MagicMock()) == ""


# ── Манифест: явные правила вместо эвристики ────────────────────────────────

def test_manifest_covers_platform_system_namespaces():
    """kube-system и metallb-system прописаны явно (confidence=1.0, source=manual).

    Нужно не для карты выше (она берёт owner из kg_services), а для
    per-service inference: `suggest_owner_multi_signal(ns, db, name=...)`
    используется в роутинге, где @mention критичен.
    """
    rules = yaml.safe_load((_REPO_ROOT / "config" / "ownership.yaml").read_text("utf-8"))
    by_ns = {r["ns_pattern"]: r["owner"] for r in rules if "ns_pattern" in r}

    assert by_ns.get("kube-system") == "@platform"
    assert by_ns.get("metallb-system") == "@platform"
    # sre-ai лежал там и раньше — фиксируем, чтобы не удалили как «дубль».
    assert by_ns.get("sre-ai") == "@platform"
