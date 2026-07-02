"""Тесты для трёх preview-регрессий, найденных в live Discord embed 2026-05-24.

См. PR: cascade deploys agg + new-baseline placeholder + multi-squad shared override.

Регрессии:
  1. Recent deploys: cascade (один build_id × N сервисов) рендерился N строк.
  2. Top alert types / firing series: без yesterday-данных вместо явного
     `(new baseline)` placeholder было тихое отсутствие Δ.
  3. Unowned namespaces: `*-shared` ns предлагал false-positive `@infra` через
     частичный prefix match вместо `multi-squad (shared, manual nudge)`.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from unittest.mock import MagicMock

import pytest

from app.services import ownership_suggester, owner_aliases, stats_digest


# ── Регрессия 1: recent_deploys cascade aggregation ────────────────────────


@pytest.fixture(autouse=True)
def _reset_caches(monkeypatch):
    """Чистим manifest/aliases между тестами."""
    ownership_suggester.reset_manifest_cache()
    owner_aliases.reset_cache()
    monkeypatch.delenv("OWNERSHIP_MANIFEST_PATH", raising=False)
    monkeypatch.delenv("OWNER_ALIASES_PATH", raising=False)
    yield
    ownership_suggester.reset_manifest_cache()
    owner_aliases.reset_cache()


def _build(
    *,
    number: str = "2138",
    triggered_by: str = "wizaryx",
    buildtype: str = "town-service",
    branch: str = "refs/heads/preprod-kingdom2",
    status: str = "SUCCESS",
    finished_at: str = "2026-05-22T08:00:00+00:00",
    build_id: int = 0,
) -> dict:
    return {
        "id": build_id,
        "number": number,
        "status": status,
        "branch": branch,
        "buildtype_name": buildtype,
        "finished_at": finished_at,
        "triggered_by": triggered_by,
        "triggered_type": "user",
    }


@pytest.mark.asyncio
async def test_recent_deploys_aggregates_cascade_same_build_number():
    """3 deploys с одинаковым #number + branch + user → 1 строка `svc1/svc2/svc3`."""
    async def fake_fetch(*, lookback_hours, limit):
        return [
            _build(buildtype="town-service", build_id=1),
            _build(buildtype="chat-tasks-service", build_id=2),
            _build(buildtype="map-service", build_id=3),
        ]
    text = await stats_digest.recent_deploys_section(fetch_fn=fake_fetch)
    assert text.count("\n  •") == 1  # одна агрегированная строка
    assert "#2138" in text
    assert "wizaryx" in text
    assert "town-service/chat-tasks-service/map-service" in text
    assert "preprod-kingdom2" in text


@pytest.mark.asyncio
async def test_recent_deploys_aggregates_caps_at_3_with_more_suffix():
    """5 сервисов в одном cascade → svc1/svc2/svc3 +2 more."""
    async def fake_fetch(*, lookback_hours, limit):
        return [
            _build(buildtype=f"svc-{i}", build_id=i) for i in range(5)
        ]
    text = await stats_digest.recent_deploys_section(fetch_fn=fake_fetch)
    assert text.count("\n  •") == 1
    assert "svc-0/svc-1/svc-2" in text
    assert "+2 more" in text


@pytest.mark.asyncio
async def test_recent_deploys_keeps_single_build_legacy_format():
    """Single build (нет cascade) рендерится старым форматом — backwards-compat."""
    async def fake_fetch(*, lookback_hours, limit):
        return [_build(buildtype="auth-service", number="500", build_id=1)]
    text = await stats_digest.recent_deploys_section(fetch_fn=fake_fetch)
    assert "auth-service" in text
    # Old format использует `({branch} #{num})`.
    assert "preprod-kingdom2 #500" in text


@pytest.mark.asyncio
async def test_recent_deploys_different_branches_not_aggregated():
    """Один user, один #number, но разные branch — это разные события."""
    async def fake_fetch(*, lookback_hours, limit):
        return [
            _build(branch="refs/heads/preprod-kingdom2", buildtype="a", build_id=1),
            _build(branch="refs/heads/preprod-kingdom3", buildtype="b", build_id=2),
        ]
    text = await stats_digest.recent_deploys_section(fetch_fn=fake_fetch)
    # Два отдельных bullet'а.
    assert text.count("\n  •") == 2


@pytest.mark.asyncio
async def test_recent_deploys_header_24h_when_builds_exist():
    """Builds за 24h → header (24h)."""
    async def fake_fetch(*, lookback_hours, limit):
        if lookback_hours == 24:
            return [_build(build_id=1)]
        return []
    text = await stats_digest.recent_deploys_section(fetch_fn=fake_fetch)
    assert "Recent deploys" in text
    assert "(24h)" in text
    assert "24h тихо" not in text


@pytest.mark.asyncio
async def test_recent_deploys_header_7d_quiet_when_24h_empty():
    """24h пусто, 7d имеет данные → header `(last 7d, 24h тихо)`."""
    call_log: list = []

    async def fake_fetch(*, lookback_hours, limit):
        call_log.append(lookback_hours)
        if lookback_hours == 24:
            return []
        # 7d window
        return [_build(build_id=1)]

    text = await stats_digest.recent_deploys_section(fetch_fn=fake_fetch)
    assert "Recent deploys" in text
    assert "24h тихо" in text
    # Был fallback-вызов на 7d.
    assert 24 in call_log
    assert any(h > 24 for h in call_log)


@pytest.mark.asyncio
async def test_recent_deploys_hidden_when_both_windows_empty():
    """24h пусто И 7d пусто → секция скрыта."""
    async def fake_fetch(*, lookback_hours, limit):
        return []
    text = await stats_digest.recent_deploys_section(fetch_fn=fake_fetch)
    assert text == ""


# ── Регрессия 2: top_alert_types `(new baseline)` placeholder ───────────


def _mk_db_for_alert_types(
    *, yest_has, today_has, yest_rows, chronic_rows, resurf_rows, today_rows=None
):
    """Builder MagicMock(Session) для top_alert_types_section.

    Порядок execute calls (M5-fix добавил today_rows для like-for-like Δ24h):
      1. yest EXISTS preflight
      2. today EXISTS preflight
      3. yest_rows fetchall (24-48h fires)
      4. today_rows fetchall (0-24h fires)
      5. chronic_rows fetchall
      6. resurf_rows fetchall
    """
    db = MagicMock()
    db.execute.side_effect = [
        MagicMock(scalar=lambda: yest_has),
        MagicMock(scalar=lambda: today_has),
        MagicMock(fetchall=lambda: yest_rows),
        MagicMock(fetchall=lambda: today_rows or []),
        MagicMock(fetchall=lambda: chronic_rows),
        MagicMock(fetchall=lambda: resurf_rows),
    ]
    return db


def test_top_alert_types_marks_new_baseline_when_no_history():
    """Нет yesterday-rows и нет today-rows → `(new baseline)`."""
    counter = Counter({"KubeDeploymentReplicasMismatch": 5})
    db = _mk_db_for_alert_types(
        yest_has=False, today_has=False,
        yest_rows=[], chronic_rows=[], resurf_rows=[],
    )
    text = stats_digest.top_alert_types_section(counter, db)
    assert "× 5" in text
    assert "new baseline" in text


def test_top_alert_types_renders_q_for_missing_yesterday():
    """Только yesterday-history отсутствует (Redis miss-аналог): `Δ24h ?`,
    chronic/resurfaced рендерим нормально из today-окна."""
    counter = Counter({"KubeDeploymentReplicasMismatch": 5})
    db = _mk_db_for_alert_types(
        yest_has=False, today_has=True,
        yest_rows=[],
        chronic_rows=[("KubeDeploymentReplicasMismatch", 5)],
        resurf_rows=[],
    )
    text = stats_digest.top_alert_types_section(counter, db)
    assert "× 5" in text
    assert "Δ24h ?" in text
    assert "5 chronic" in text


def test_top_alert_types_does_not_falsely_mark_new_baseline_when_legit_zero():
    """В yesterday-окне есть rows, просто наш alertname не fired (0), а сегодня
    5 fires → Δ24h +5 (today 5 − yesterday 0), НЕ `new baseline`."""
    counter = Counter({"NewAlert": 5})
    db = _mk_db_for_alert_types(
        yest_has=True, today_has=True,
        yest_rows=[("OtherAlert", 3)],  # был, но не наш → yesterday=0
        today_rows=[("NewAlert", 5)],   # сегодня 5 fires → Δ = +5
        chronic_rows=[], resurf_rows=[],
    )
    text = stats_digest.top_alert_types_section(counter, db)
    assert "Δ24h +5" in text
    assert "new baseline" not in text


def test_firing_series_trend_new_baseline_marker():
    """Yesterday=None → `(new baseline)` (sanity на helper, см. UX-тест)."""
    s = stats_digest._fmt_firing_series_trend(673, None)
    assert "new baseline" in s


# ── Регрессия 3: bare `*-shared` ns → multi-squad ──────────────────────


def test_multi_signal_bare_preprod_shared_suggests_multi_squad():
    """Bare `preprod-shared` без deploy-history/labels → multi-squad."""
    sug = ownership_suggester.suggest_owner_multi_signal("preprod-shared", db=None)
    assert sug.owner == "multi-squad"
    # Confidence медиум — позволяет manual / deploy_history override.
    assert sug.confidence < 0.8


def test_multi_signal_bare_prod_shared_suggests_multi_squad():
    sug = ownership_suggester.suggest_owner_multi_signal("prod-shared", db=None)
    assert sug.owner == "multi-squad"


def test_multi_signal_squad_n_shared_still_resolves_to_squad():
    """`squad-7-shared` НЕ multi-squad — это конкретный squad-7."""
    sug = ownership_suggester.suggest_owner_multi_signal("squad-7-shared", db=None)
    assert sug.owner == "squad-7"


def test_multi_signal_env_kingdom_not_multi_squad():
    """`<env>-kingdom<N>` это конкретный realm, НЕ multi-squad."""
    sug = ownership_suggester.suggest_owner_multi_signal("prod-kingdom2", db=None)
    assert sug.owner == "kingdom2"
    sug2 = ownership_suggester.suggest_owner_multi_signal("preprod-kingdom3", db=None)
    assert sug2.owner == "kingdom3"


def test_multi_signal_bare_shared_can_be_overridden_by_deploy_history():
    """Если deploy_history говорит «kemyashev регулярно деплоит в prod-shared» —
    его @squad-1 должен победить multi-squad placeholder."""
    from tests.test_owner_inference_multi import _mock_db_with_responses
    db = _mock_db_with_responses(
        deploys=[("kemyashev", 20)],  # strength=1.0 → 0.4
        labels=[],
    )
    sug = ownership_suggester.suggest_owner_multi_signal("prod-shared", db)
    # B (0.4 * 1.0 = 0.4) > A multi-squad (0.4 * 0.6 = 0.24).
    assert sug.owner == "squad-1"


def test_multi_signal_bare_shared_overridden_by_manifest(monkeypatch, tmp_path):
    """Manual manifest всегда побеждает multi-squad-плейсхолдер."""
    manifest = tmp_path / "ownership.yaml"
    manifest.write_text(
        "- ns_pattern: \"prod-shared\"\n"
        "  owner: \"@platform\"\n"
    )
    monkeypatch.setenv("OWNERSHIP_MANIFEST_PATH", str(manifest))
    ownership_suggester.reset_manifest_cache()
    sug = ownership_suggester.suggest_owner_multi_signal("prod-shared", db=None)
    assert sug.owner == "platform"
    assert sug.manual is True


def test_unowned_section_renders_multi_squad_with_manual_nudge():
    """Рендер `*-shared` ns с пометкой `(shared, manual nudge)`."""
    unowned: defaultdict = defaultdict(int)
    unowned["preprod-shared"] = 4
    text = stats_digest.unowned_namespaces_section(unowned, db=None)
    assert "multi-squad" in text
    assert "shared, manual nudge" in text
    # False-positive @infra не должен здесь появиться.
    assert "@infra" not in text


def test_unowned_section_kingdom_still_renders_kingdom_owner():
    """Sanity-check что мы не сломали kingdom-render."""
    unowned: defaultdict = defaultdict(int)
    unowned["prod-kingdom2"] = 10
    text = stats_digest.unowned_namespaces_section(unowned, db=None)
    assert "@kingdom2" in text
    assert "multi-squad" not in text


def test_legacy_suggest_owner_for_ns_unchanged_for_shared():
    """Legacy API остаётся stable: prod-shared → shared (а не multi-squad).

    Это сознательное решение — не ломать internal callers. Multi-squad —
    только в multi_signal pipeline-е (новый API).
    """
    assert ownership_suggester.suggest_owner_for_ns("prod-shared") == "shared"
    assert ownership_suggester.suggest_owner_for_ns("preprod-shared") == "shared"
