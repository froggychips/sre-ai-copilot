"""Тесты для DQ polish 2026-05-25 stats_digest:

1. MTTR winsorize: durations >7d не попадают в median/p95, отдельный counter.
2. Deploy correlation diagnostics: при attributed=0 + N>=10 рисуется warning
   с % linked deploys и % linked alerts.
3. Anomalies degrade при stale metrics: >60m показывает warning, >4h
   скрывает секцию.
4. Suspicious stale drill-down: 4 buckets (prod-with-alerts / callers /
   external-mcp / batch sweep) суммируют в total.
5. Noisemaker format: `@<ns>` вместо italic `_ns_`, пустой ns — без `@`.

Все тесты используют MagicMock без живого DB.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.services import stats_digest


# ── 1. MTTR winsorize ──────────────────────────────────────────────────────


def test_mttr_stats_winsorize_excludes_outliers_from_p95():
    """Прод 25 мая: median 39 / p95 60891 (42d). Winsorize должен дать
    sane p95 в районе 47-100min и отдельный outliers count."""
    db = MagicMock()
    # SQL теперь возвращает 4 cols: median, p95, samples (<7d), outliers (>=7d)
    db.execute.return_value.fetchone.return_value = (39.0, 47.0, 316, 12)
    stats = stats_digest._mttr_stats(db, days=7)
    assert stats is not None
    assert stats["median_min"] == 39.0
    assert stats["p95_min"] == 47.0  # winsorized — не 60891
    assert stats["samples"] == 316
    assert stats["outliers_gt_7d"] == 12


def test_mttr_section_renders_outliers_when_present():
    """outliers > 0 → строка содержит `outliers (>7d): N`."""
    db = MagicMock()
    # current call + previous call (days*2 window)
    db.execute.return_value.fetchone.side_effect = [
        (39.0, 47.0, 316, 12),  # current
        (40.0, 50.0, 600, 25),  # prev (14d window includes current)
    ]
    text = stats_digest.mttr_section(db, days=7)
    assert "MTTR" in text
    assert "39min" in text
    assert "47min" in text
    assert "outliers (>7d)" in text
    assert "12" in text


def test_mttr_section_no_outliers_string_when_zero():
    """outliers=0 → строка без сегмента outliers."""
    db = MagicMock()
    db.execute.return_value.fetchone.side_effect = [
        (10.0, 50.0, 100, 0),  # current — нет outliers
        (12.0, 55.0, 200, 0),  # prev
    ]
    text = stats_digest.mttr_section(db, days=7)
    assert "outliers" not in text


def test_mttr_stats_returns_none_when_no_samples_and_no_outliers():
    db = MagicMock()
    db.execute.return_value.fetchone.return_value = (None, None, 0, 0)
    stats = stats_digest._mttr_stats(db, days=7)
    assert stats is None


# ── 2. Deploy correlation diagnostics ──────────────────────────────────────


def test_deploy_correlation_renders_diagnostic_when_attributed_zero():
    """1834 deploys · 0 attributed → diagnostic-line с % linked."""
    db = MagicMock()
    # overall + worst + diagnostic
    db.execute.return_value.fetchone.side_effect = [
        (1834, 0, 1045),  # total=1834, attributed=0, successes=1045
        None,  # worst — нет alerts → нет ряда
        # diagnostic: d_total, d_linked, d_svc, a_total, a_linked, a_svc, overlap
        (1834, 257, 200, 9000, 3400, 80, 5),
    ]
    text = stats_digest.deploy_incident_correlation_section(db, hours=24)
    assert "Deploy" in text
    assert "1834" in text
    # linked% низкие (14%) → ветка linkage gap, не «нет пересечения»
    assert "Likely linkage gap" in text
    assert "14%" in text  # 257 / 1834 ≈ 14%
    assert "deploys linked" in text
    # 3400 / 9000 ≈ 37.8% → rounds to 38%
    assert "38%" in text or "37%" in text
    assert "alerts linked" in text


def test_deploy_correlation_renders_disjoint_when_linked_but_no_overlap():
    """attributed=0, но service_id есть с обеих сторон и overlap=0 →
    это НЕ linkage-баг, а непересекающиеся множества."""
    db = MagicMock()
    db.execute.return_value.fetchone.side_effect = [
        (795, 0, 795),  # total=795, attributed=0
        None,  # worst
        # 100% linked с обеих сторон, но overlap=0
        (795, 795, 265, 77, 77, 32, 0),
    ]
    text = stats_digest.deploy_incident_correlation_section(db, hours=24)
    assert "Likely linkage gap" not in text
    assert "Нет пересечения" in text
    assert "265" in text and "32" in text


def test_deploy_correlation_no_diagnostic_when_attributed_positive():
    """attributed > 0 → diagnostic не рисуется."""
    db = MagicMock()
    db.execute.return_value.fetchone.side_effect = [
        (100, 30, 75),  # total=100, attributed=30
        None,  # worst
    ]
    text = stats_digest.deploy_incident_correlation_section(db, hours=24)
    assert "Likely linkage gap" not in text
    assert "100" in text
    assert "30" in text


def test_deploy_correlation_no_diagnostic_when_total_too_small():
    """attributed=0 но total<10 → diagnostic не рисуется (могут быть real
    deploy-free часы)."""
    db = MagicMock()
    db.execute.return_value.fetchone.side_effect = [
        (5, 0, 5),
        None,
    ]
    text = stats_digest.deploy_incident_correlation_section(db, hours=24)
    assert "Likely linkage gap" not in text


def test_deploy_correlation_diagnostics_helper_computes_percentages():
    db = MagicMock()
    # d_total, d_linked, d_svc, a_total, a_linked, a_svc, overlap
    db.execute.return_value.fetchone.return_value = (1000, 200, 50, 5000, 1500, 40, 7)
    diag = stats_digest._deploy_correlation_diagnostics(db, hours=24)
    assert diag["deploys_linked_pct"] == 20.0
    assert diag["alerts_linked_pct"] == 30.0
    assert diag["deploy_svc"] == 50
    assert diag["alert_svc"] == 40
    assert diag["overlap"] == 7


def test_deploy_correlation_diagnostics_helper_safe_on_error():
    db = MagicMock()
    db.execute.side_effect = RuntimeError("DB down")
    diag = stats_digest._deploy_correlation_diagnostics(db, hours=24)
    assert diag["deploys_linked_pct"] == 0.0
    assert diag["alerts_linked_pct"] == 0.0
    assert diag["overlap"] == 0


# ── 3. Anomalies degrade при stale metrics ─────────────────────────────────


def _make_fake_sync_lag(lag_minutes_for_metrics: float):
    """Helper: возвращает CheckResult-like объект для mock check_sync_lag."""
    from app.knowledge_graph.self_health import CheckResult
    return CheckResult(
        name="sync_lag",
        status="warn",
        detail={
            "per_task": {
                "kg_metrics_sync": {
                    "lag_minutes": lag_minutes_for_metrics,
                    "status": "warn",
                    "last_ts": "2026-05-25T08:30:00",
                    "expected_interval_minutes": 10,
                },
            }
        },
    )


def test_anomalies_renders_degraded_when_metrics_stale_over_threshold():
    """metrics sync lag = 120 min (>60 threshold) → header содержит warning."""
    db = MagicMock()
    db.execute.return_value.fetchone.return_value = (14367, 200)
    db.execute.return_value.fetchall.return_value = []  # severity / top / metric

    with patch(
        "app.knowledge_graph.self_health.check_sync_lag",
        return_value=_make_fake_sync_lag(120.0),
    ):
        text = stats_digest.anomaly_summary_section(db)
    assert "Anomalies" in text
    assert "stale" in text
    assert "metrics sync" in text
    # 120min = 2.0h
    assert "2.0h ago" in text or "2.0 h" in text or "2.0h" in text
    # Counts всё равно показываем
    assert "14367" in text or "14,367" in text


def test_anomalies_skipped_when_metrics_severely_stale():
    """lag > 4× threshold (>240 min by default) → одна строка `skipped`."""
    db = MagicMock()
    # На этом пути SQL не должен вызываться вообще
    db.execute.return_value.fetchone.return_value = (14367, 200)

    with patch(
        "app.knowledge_graph.self_health.check_sync_lag",
        return_value=_make_fake_sync_lag(300.0),  # 5h
    ):
        text = stats_digest.anomaly_summary_section(db)
    assert "Anomalies" in text
    assert "skipped" in text
    assert "5.0h" in text


def test_anomalies_normal_when_metrics_fresh():
    """lag < threshold → стандартный header без warning."""
    db = MagicMock()
    db.execute.return_value.fetchone.return_value = (50, 10)
    db.execute.return_value.fetchall.return_value = []

    with patch(
        "app.knowledge_graph.self_health.check_sync_lag",
        return_value=_make_fake_sync_lag(15.0),  # 15 min < 60
    ):
        text = stats_digest.anomaly_summary_section(db)
    assert "stale" not in text
    assert "50" in text


def test_anomalies_normal_when_sync_lag_check_unavailable():
    """check_sync_lag упал → degrade пропускаем (None lag), секция как раньше."""
    db = MagicMock()
    db.execute.return_value.fetchone.return_value = (5, 2)
    db.execute.return_value.fetchall.return_value = []
    with patch(
        "app.knowledge_graph.self_health.check_sync_lag",
        side_effect=RuntimeError("self-health down"),
    ):
        text = stats_digest.anomaly_summary_section(db)
    assert "stale" not in text
    assert "skipped" not in text
    assert "5" in text


# ── 4. Suspicious stale drill-down ─────────────────────────────────────────


def test_suspicious_stale_drill_down_renders_buckets():
    db = MagicMock()
    # chronic fetchall, plus drill-down for prod_with_alerts
    # action_items_section calls:
    #   _chronic_action_items → fetchall
    #   _unowned_action_items → scalar
    #   _suspicious_stale_action_items → scalar
    #   _suspicious_in_prod_with_alerts → fetchall
    #   _suspicious_with_callers → scalar
    #   _suspicious_in_external_or_mcp → scalar
    db.execute.return_value.fetchall.side_effect = [
        [],  # chronic_action_items empty
        # prod_with_alerts — (name, namespace)
        [
            ("payments-svc", "prod-kingdom1"),
            ("auth-svc", "prod-kingdom2"),
            ("billing-svc", "prod-shared"),
        ],
    ]
    db.execute.return_value.scalar.side_effect = [
        10,  # unowned
        2090,  # stale total
        312,  # with_callers
        156,  # external_or_mcp
    ]
    text = stats_digest.action_items_section(db)
    assert "Action items" in text
    assert "2090" in text
    assert "suspicious_stale" in text
    assert "in prod/*" in text or "prod" in text
    assert "3" in text  # prod count = 3 names
    assert "payments-svc" in text  # top sample
    assert "312" in text
    assert "inbound_callers" in text
    assert "156" in text
    assert "external/mcp" in text or "external" in text
    # remaining = 2090 - 3 - 312 - 156 = 1619
    assert "1619" in text
    assert "batch sweep" in text


def test_suspicious_in_prod_with_alerts_helper():
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [
        ("svc-1", "prod-k1"), ("svc-2", "prod-k2"), ("svc-3", "prod-k3"),
        ("svc-4", "prod-k4"), ("svc-5", "prod-k5"),
    ]
    cnt, top = stats_digest._suspicious_in_prod_with_alerts(db, days=60)
    assert cnt == 5
    # top — (name, namespace): ns обязателен, одноимённые сервисы из разных
    # ns иначе неразличимы.
    assert top == [("svc-1", "prod-k1"), ("svc-2", "prod-k2"), ("svc-3", "prod-k3")]


def test_suspicious_remaining_is_non_negative():
    """При пересекающихся buckets — leftover не уходит в минус."""
    db = MagicMock()
    leftover = stats_digest._suspicious_remaining(
        db, total=100, prod_with_alerts=50, with_callers=60, external_or_mcp=20
    )
    assert leftover == 0  # 100 - 130 = -30 → clamp to 0


def test_suspicious_remaining_basic_math():
    db = MagicMock()
    leftover = stats_digest._suspicious_remaining(
        db, total=2090, prod_with_alerts=47, with_callers=312, external_or_mcp=156
    )
    assert leftover == 2090 - 47 - 312 - 156


# ── 5. Noisemaker @ns format ───────────────────────────────────────────────


def test_noisemaker_uses_at_ns_format():
    """`@<ns>` маркер вместо italic `_<ns>_`."""
    fired = [{"metric": {"namespace": "mcp", "service": "vm-kube-state-metrics"}}] * 25
    fired += [{"metric": {"namespace": "other", "service": "svc-x"}}] * 75
    text = stats_digest.noisemakers_section(fired, threshold_pct=20.0)
    assert "Noisemakers" in text
    assert "@mcp" in text
    assert "_mcp_" not in text  # старый формат не появляется
    assert "vm-kube-state-metrics" in text


def test_noisemaker_no_at_marker_when_ns_empty():
    """Если namespace нет (или '?') — `@` маркер не рисуется."""
    fired = [
        {"metric": {"service": "lonely-svc"}}
    ] * 50  # 100% dominance, без ns
    text = stats_digest.noisemakers_section(fired, threshold_pct=20.0)
    assert "lonely-svc" in text
    assert "@" not in text
