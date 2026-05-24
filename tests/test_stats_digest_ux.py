"""Тесты для UX-полировки stats_digest (6 items batch).

Каждый item получает свой блок:
  1. series unit (не cyrillic «с»)         → test_firing_alerts_series_*
  2. unowned namespaces секция + suggest    → test_unowned_*, test_ownership_*
  3. firing-series trend vs yesterday       → test_firing_trend_*, test_cluster_*
  4. top_alert_types Δ24h + chronic/resurf  → test_top_alert_types_metadata_*
  5. stale classification (expected vs susp)→ test_classify_stale_*, test_stale_*
  6. blast-radius rename + fragile filter   → перекрывается в test_stats_digest.py
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, AsyncMock

import pytest

from app.services import stats_digest, ownership_suggester


# ── Item #1: «series» вместо cyrillic «с» ─────────────────────────────────


def test_firing_alerts_series_unit_in_team_line():
    fired = [
        {"metric": {"namespace": "prod-kingdom1", "alertname": "X"}}
        for _ in range(226)
    ]
    ns_to_team = {"prod-kingdom1": "kingdom1"}
    text, _, _, _ = stats_digest.firing_alerts_section(fired, ns_to_team)
    assert "226 series" in text
    # Cyrillic «с» — типичный bug, ловится глазом как «секунды»
    assert "226с" not in text


def test_firing_alerts_series_unit_multi_team():
    fired = (
        [{"metric": {"namespace": "prod-kingdom1", "alertname": "X"}}] * 5
        + [{"metric": {"namespace": "prod-shared", "alertname": "Y"}}] * 3
    )
    ns_to_team = {"prod-kingdom1": "kingdom1", "prod-shared": "shared"}
    text, _, _, _ = stats_digest.firing_alerts_section(fired, ns_to_team)
    assert "5 series" in text
    assert "3 series" in text


# ── Item #2: ownership_suggester ───────────────────────────────────────────


def test_ownership_suggester_squad_prefix():
    assert ownership_suggester.suggest_owner_for_ns("squad-7-shared") == "squad-7"
    assert ownership_suggester.suggest_owner_for_ns("squad-23-kingdom2") == "squad-23"


def test_ownership_suggester_env_kingdom():
    assert ownership_suggester.suggest_owner_for_ns("prod-kingdom1") == "kingdom1"
    assert ownership_suggester.suggest_owner_for_ns("preprod-kingdom2") == "kingdom2"
    assert ownership_suggester.suggest_owner_for_ns("dev-kingdom3") == "kingdom3"


def test_ownership_suggester_env_shared():
    assert ownership_suggester.suggest_owner_for_ns("prod-shared") == "shared"
    assert ownership_suggester.suggest_owner_for_ns("preprod-shared") == "shared"


def test_ownership_suggester_env_realm_aliases():
    assert ownership_suggester.suggest_owner_for_ns("prod-data") == "data"
    assert ownership_suggester.suggest_owner_for_ns("prod-cdn") == "cdn"
    assert ownership_suggester.suggest_owner_for_ns("preprod-payments") == "payments"


def test_ownership_suggester_bare_platform_namespaces():
    """`monitoring`, `kube-system`, `cert-manager` → platform."""
    assert ownership_suggester.suggest_owner_for_ns("monitoring") == "platform"
    assert ownership_suggester.suggest_owner_for_ns("kube-system") == "platform"
    assert ownership_suggester.suggest_owner_for_ns("cert-manager") == "platform"
    assert ownership_suggester.suggest_owner_for_ns("ingress-nginx") == "platform"


def test_ownership_suggester_kg_fallback_when_prefix_unknown():
    """ns не подходит ни под одну regex-эвристику, но в kg_services есть
    сервис с team_owner — отдаём этот team_owner."""
    db = MagicMock()
    db.execute.return_value.fetchone.return_value = ("infra-tools",)
    result = ownership_suggester.suggest_owner_for_ns("weird-ns-name", db)
    assert result == "infra-tools"


def test_ownership_suggester_returns_none_when_nothing_matches():
    db = MagicMock()
    db.execute.return_value.fetchone.return_value = None
    result = ownership_suggester.suggest_owner_for_ns("totally-unknown-ns", db)
    assert result is None


def test_ownership_suggester_returns_none_for_empty():
    assert ownership_suggester.suggest_owner_for_ns("") is None
    assert ownership_suggester.suggest_owner_for_ns("(no-ns)") is None


def test_ownership_suggester_bulk_prefix_matches_avoid_db_call():
    """Все ns matchatся через regex → SQL не вызывается."""
    db = MagicMock()
    result = ownership_suggester.suggest_owners_bulk(
        ["prod-kingdom1", "prod-shared", "squad-7-shared"],
        db,
    )
    # Если все matchnulis по prefix — db.execute не должен быть вызван.
    db.execute.assert_not_called()
    assert result == {
        "prod-kingdom1": "kingdom1",
        "prod-shared": "shared",
        "squad-7-shared": "squad-7",
    }


def test_ownership_suggester_bulk_mixes_prefix_and_kg():
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [
        ("weird-ns", "infra-team"),
    ]
    result = ownership_suggester.suggest_owners_bulk(
        ["prod-kingdom1", "weird-ns", "unknown-ns"],
        db,
    )
    assert result["prod-kingdom1"] == "kingdom1"  # prefix match
    assert result["weird-ns"] == "infra-team"     # kg lookup
    assert result["unknown-ns"] is None           # no match


# ── Item #2: unowned_namespaces_section ────────────────────────────────────


def test_unowned_namespaces_section_renders_top_with_suggestions():
    """unowned dict → секция «🔎 Unowned namespaces» с suggest для каждого."""
    unowned: defaultdict = defaultdict(int)
    unowned["monitoring"] = 44
    unowned["squad-7-kingdom2"] = 36
    unowned["prod-shared"] = 28  # этот должен был быть owned, тестируем suggest
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []  # bulk KG → ничего

    text = stats_digest.unowned_namespaces_section(unowned, db)
    assert "Unowned namespaces" in text
    assert "monitoring" in text
    assert "44 series" in text
    assert "squad-7-kingdom2" in text
    assert "suggest" in text
    # Префикс-эвристика должна сработать для monitoring → @platform
    assert "@platform" in text
    # Для squad-7-kingdom2 → @squad-7
    assert "@squad-7" in text


def test_unowned_namespaces_section_renders_question_for_unknown():
    unowned: defaultdict = defaultdict(int)
    unowned["totally-random-ns"] = 5
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []
    text = stats_digest.unowned_namespaces_section(unowned, db)
    assert "?" in text


def test_unowned_namespaces_section_hidden_when_empty():
    """Пустой unowned → секция скрыта (return '')."""
    text = stats_digest.unowned_namespaces_section(defaultdict(int), db=None)
    assert text == ""


def test_unowned_namespaces_caps_at_top_n():
    unowned: defaultdict = defaultdict(int)
    for i in range(20):
        unowned[f"ns-{i}"] = i + 1
    text = stats_digest.unowned_namespaces_section(unowned, db=None, top_n=5)
    # Только 5 bullet-строк
    bullet_count = text.count("\n  •")
    assert bullet_count == 5


# ── Item #3: firing-series trend vs yesterday ─────────────────────────────


def test_firing_series_trend_new_baseline():
    """Yesterday=None → `(new baseline)` метка."""
    s = stats_digest._fmt_firing_series_trend(673, None)
    assert "new baseline" in s


def test_firing_series_trend_positive_delta_with_pct():
    s = stats_digest._fmt_firing_series_trend(720, 673)
    assert "+47" in s
    assert "vs вчера" in s
    assert "7.0%" in s or "+7.0%" in s


def test_firing_series_trend_negative_delta():
    s = stats_digest._fmt_firing_series_trend(600, 700)
    assert "-100" in s
    # Знак минуса не должен превратиться в `+`
    assert "+-" not in s


def test_firing_series_trend_zero_delta():
    s = stats_digest._fmt_firing_series_trend(500, 500)
    assert "=0" in s or "0 vs вчера" in s


def test_firing_series_trend_yesterday_zero_avoids_div_by_zero():
    """Yesterday=0, today>0 — не делим на ноль, просто +N."""
    s = stats_digest._fmt_firing_series_trend(50, 0)
    assert "+50" in s
    # Никаких NaN / ZeroDivisionError trace в строке
    assert "Inf" not in s and "NaN" not in s


@pytest.mark.asyncio
async def test_cluster_health_renders_firing_series_trend():
    vm = MagicMock()
    vm.get_cluster_health = AsyncMock(return_value=MagicMock(
        to_dict=lambda: {"nodes_ready": 16, "crashloops": 5}
    ))
    text = await stats_digest.cluster_health_section(
        vm, fired_series=[{}] * 720, firing_series_yesterday=673,
    )
    assert "`720`" in text
    assert "+47" in text


@pytest.mark.asyncio
async def test_cluster_health_baseline_when_no_yesterday():
    vm = MagicMock()
    vm.get_cluster_health = AsyncMock(return_value=MagicMock(
        to_dict=lambda: {"nodes_ready": 16, "crashloops": 5}
    ))
    text = await stats_digest.cluster_health_section(
        vm, fired_series=[{}] * 500, firing_series_yesterday=None,
    )
    assert "`500`" in text
    assert "new baseline" in text


# ── Item #4: top_alert_types Δ24h + chronic + resurfaced ──────────────────


def test_top_alert_types_renders_delta_and_chronic_resurfaced():
    counter = Counter({
        "KubeDeploymentReplicasMismatch": 75,
        "KubePodCrashLooping": 30,
    })
    db = MagicMock()
    # yest_rows → chronic_rows → resurf_rows
    db.execute.side_effect = [
        MagicMock(fetchall=lambda: [
            ("KubeDeploymentReplicasMismatch", 63),
            ("KubePodCrashLooping", 25),
        ]),
        MagicMock(fetchall=lambda: [
            ("KubeDeploymentReplicasMismatch", 23),
            ("KubePodCrashLooping", 12),
        ]),
        MagicMock(fetchall=lambda: [
            ("KubeDeploymentReplicasMismatch", 8),
        ]),
    ]
    text = stats_digest.top_alert_types_section(counter, db)
    assert "`KubeDeploymentReplicasMismatch` × 75" in text
    assert "Δ24h +12" in text  # 75 - 63
    assert "23 chronic" in text
    assert "8 resurfaced" in text
    # У KubePodCrashLooping resurfaced=0 → не показываем эту часть
    assert "12 chronic" in text


def test_top_alert_types_backwards_compat_without_db():
    """Без db — базовый формат (важно для существующих тестов)."""
    counter = Counter({"X": 10, "Y": 5})
    text = stats_digest.top_alert_types_section(counter)
    assert "`X` × 10" in text
    # Никаких Δ-фрагментов
    assert "Δ24h" not in text
    assert "chronic" not in text


def test_top_alert_types_db_error_falls_back_to_base_format():
    counter = Counter({"X": 10})
    db = MagicMock()
    db.execute.side_effect = RuntimeError("table kg_alerts missing")
    text = stats_digest.top_alert_types_section(counter, db)
    assert "`X` × 10" in text


# ── Item #5: stale classification ──────────────────────────────────────────


def test_classify_stale_backup_suffix_is_expected():
    assert stats_digest._classify_stale("postgres-backup", "prod-kingdom1") == "expected"
    assert stats_digest._classify_stale("chat-messages-additional-backup", "prod-shared") == "expected"


def test_classify_stale_cron_suffix_is_expected():
    assert stats_digest._classify_stale("nightly-cron", "prod-kingdom1") == "expected"
    assert stats_digest._classify_stale("etcd-snapshot-cronjob", "prod-shared") == "expected"


def test_classify_stale_system_namespace_is_expected():
    assert stats_digest._classify_stale("coredns", "kube-system") == "expected"
    assert stats_digest._classify_stale("prometheus", "monitoring") == "expected"
    assert stats_digest._classify_stale("nginx-ingress-controller", "ingress-nginx") == "expected"


def test_classify_stale_application_is_suspicious():
    assert stats_digest._classify_stale("auth-service", "prod-shared") == "suspicious"
    assert stats_digest._classify_stale("town-service", "prod-kingdom1") == "suspicious"


def test_stale_section_hides_expected_by_default():
    """Backup deployments не должны попадать в основной список."""
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [("prod-kingdom1",)]
    now = datetime.now(timezone.utc)
    fakes = [
        {  # expected — должен быть скрыт
            "metadata": {"name": "town-db-backup", "creationTimestamp": (now - timedelta(days=62)).isoformat()},
            "status": {"readyReplicas": 1, "conditions": [
                {"lastUpdateTime": (now - timedelta(days=62)).isoformat()}
            ]},
        },
        {  # suspicious — должен быть показан
            "metadata": {"name": "auth-service", "creationTimestamp": (now - timedelta(days=45)).isoformat()},
            "status": {"readyReplicas": 1, "conditions": [
                {"lastUpdateTime": (now - timedelta(days=45)).isoformat()}
            ]},
        },
    ]
    text = stats_digest.stale_deployments_section(
        db, ns_to_team={"prod-kingdom1": "kingdom1"}, threshold_days=30,
        kubectl_fn=lambda ns: fakes, hide_expected=True,
    )
    assert "auth-service" in text
    assert "town-db-backup" not in text
    # Pill про скрытые expected должен присутствовать
    assert "expected" in text


def test_stale_section_shows_all_when_hide_expected_false():
    """Override флага: показываем backup-deployments тоже."""
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [("prod-kingdom1",)]
    now = datetime.now(timezone.utc)
    fakes = [{
        "metadata": {"name": "town-db-backup", "creationTimestamp": (now - timedelta(days=62)).isoformat()},
        "status": {"readyReplicas": 1, "conditions": [
            {"lastUpdateTime": (now - timedelta(days=62)).isoformat()}
        ]},
    }]
    text = stats_digest.stale_deployments_section(
        db, ns_to_team={"prod-kingdom1": "kingdom1"}, threshold_days=30,
        kubectl_fn=lambda ns: fakes, hide_expected=False,
    )
    assert "town-db-backup" in text


def test_stale_section_empty_with_only_expected_hidden():
    """Все entries expected → основной список пуст, но pill показывается."""
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [("prod-kingdom1",)]
    now = datetime.now(timezone.utc)
    fakes = [{
        "metadata": {"name": "nightly-backup", "creationTimestamp": (now - timedelta(days=62)).isoformat()},
        "status": {"readyReplicas": 1, "conditions": [
            {"lastUpdateTime": (now - timedelta(days=62)).isoformat()}
        ]},
    }] * 3
    text = stats_digest.stale_deployments_section(
        db, ns_to_team={"prod-kingdom1": "kingdom1"}, threshold_days=30,
        kubectl_fn=lambda ns: fakes, hide_expected=True,
    )
    assert "ничего suspicious" in text
    assert "скрыто `3`" in text or "3 expected" in text
