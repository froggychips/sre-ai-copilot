"""Регрессионные тесты для bug-fixes в stats_digest (review-fixes).

Покрытие:
  * M2 — `_suspicious_with_callers` использует правильную таблицу/колонку
    edge-ей (kg_service_edges / dst_id, а не несуществующую kg_edges/target_id)
    и возвращает count, а не молча 0.
  * M3 — изоляция под-секций дайджеста: упавший запрос ловится, транзакция
    откатывается (rollback) и не роняет каскадом следующие секции (общий
    Session). Проверяем anomaly_summary_section, _count_alerts_in_window,
    _alert_type_metadata.
  * M4 — distinct stale-деплой, делящий namespace со свёрнутой группой, не
    выпадает из хвоста (фильтр по (name, ns), а не по namespace).
  * M5 — (a) Δ24h сравнивает like-for-like (today-fires vs yesterday-fires),
    (b) MTTR trend сравнивает предыдущее НЕпересекающееся окно той же длины.

Все тесты на MagicMock — без живого PostgreSQL. Для M2/M4 PG-специфичный SQL
не гоняется, поэтому проверяем сам SQL-текст и поведение на моках.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.services import stats_digest


# ── M2. suspicious_with_callers: правильная edge-таблица ────────────────────


def test_suspicious_with_callers_uses_kg_service_edges_and_returns_count():
    """SQL должен ссылаться на kg_service_edges/dst_id (как остальные edge-
    запросы файла), а не на несуществующую kg_edges/target_id — иначе запрос
    падал, глотался и молча возвращал 0."""
    db = MagicMock()
    db.execute.return_value.scalar.return_value = 312
    cnt = stats_digest._suspicious_with_callers(db, days=60)

    assert cnt == 312  # count вернулся, не 0 от swallowed exception

    sql = str(db.execute.call_args[0][0])
    assert "kg_service_edges" in sql
    assert "dst_id" in sql
    # Старые (неверные) имена не должны присутствовать.
    assert "kg_edges" not in sql  # "kg_edges" не substring "kg_service_edges"
    assert "target_id" not in sql


def test_suspicious_with_callers_swallows_and_rolls_back_on_error():
    """При падении запроса — возвращаем 0 И откатываем транзакцию."""
    db = MagicMock()
    db.execute.side_effect = RuntimeError("boom")
    cnt = stats_digest._suspicious_with_callers(db, days=60)
    assert cnt == 0
    assert db.rollback.called


# ── M3. Изоляция под-секций: rollback + не пробрасываем исключение ──────────


def test_anomaly_summary_subquery_failure_is_isolated():
    """Падение by_severity под-запроса не роняет секцию: она возвращает
    контент с fallback-значениями и откатывает транзакцию (иначе следующие
    секции дайджеста упали бы каскадом на InFailedSqlTransaction)."""
    db = MagicMock()
    # total → by_severity(raise) → top_services → by_metric
    db.execute.side_effect = [
        MagicMock(fetchone=lambda: (47, 12)),          # total, distinct
        RuntimeError("severity query blew up"),        # by_severity падает
        MagicMock(fetchall=lambda: [("mv-service", "prod-shared", 12)]),  # top_services
        MagicMock(fetchall=lambda: [("p95_latency_ms", 20)]),  # by_metric
    ]
    with patch.object(stats_digest, "_metrics_sync_lag_minutes", return_value=None):
        text = stats_digest.anomaly_summary_section(db)

    # Секция отрендерилась, не подняла исключение.
    assert "Total: 47" in text
    assert "12 svc" in text
    # by_severity упал → fallback 0/0, но остальное на месте.
    assert "warning: 0" in text
    assert "critical: 0" in text
    assert "mv-service" in text
    # Транзакция откатана.
    assert db.rollback.called


def test_anomaly_summary_first_query_failure_rolls_back():
    """Даже первый (total) guard теперь откатывает транзакцию перед '' —
    иначе секция скрывалась, но Session оставался poisoned для следующих."""
    db = MagicMock()
    db.execute.side_effect = RuntimeError("relation kg_anomaly_observations does not exist")
    with patch.object(stats_digest, "_metrics_sync_lag_minutes", return_value=None):
        text = stats_digest.anomaly_summary_section(db)
    assert text == ""
    assert db.rollback.called


def test_count_alerts_in_window_rolls_back_on_error():
    db = MagicMock()
    db.execute.side_effect = RuntimeError("kg_alerts missing")
    fired, resolved = stats_digest._count_alerts_in_window(db, hours=24)
    assert (fired, resolved) == (0, 0)
    assert db.rollback.called


def test_alert_type_metadata_rolls_back_on_error():
    db = MagicMock()
    db.execute.side_effect = RuntimeError("kg_alerts missing")
    out = stats_digest._alert_type_metadata(db, ["KubePodCrashLooping"])
    assert out == {}
    assert db.rollback.called


# ── M4. Distinct stale-деплой, делящий namespace со свёрнутой группой ───────


def _dep(name: str, last_iso: str, replicas: int = 1) -> dict:
    return {
        "metadata": {
            "name": name,
            "annotations": {"meta.helm.sh/release-name": name},
            "creationTimestamp": last_iso,
        },
        "status": {
            "readyReplicas": replicas,
            "conditions": [
                {"lastUpdateTime": last_iso, "type": "Available", "status": "True"}
            ],
        },
    }


def test_stale_singular_not_dropped_when_shares_ns_with_compacted_group():
    """`shared-app` в 3 ns (idle 62d) сворачивается в одну строку. Отдельный
    `lonely-svc` живёт в prod-kingdom1 (idle 40d) — namespace тот же, что у
    свёрнутой группы. Раньше фильтр по namespace выкидывал его; теперь
    фильтр по (name, ns) — он остаётся в хвосте."""
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [
        ("prod-kingdom1",), ("prod-kingdom2",), ("prod-kingdom3",),
    ]
    now = datetime.now(timezone.utc)
    group_iso = (now - timedelta(days=62)).isoformat()
    lonely_iso = (now - timedelta(days=40)).isoformat()
    fake = {
        "prod-kingdom1": [_dep("shared-app", group_iso), _dep("lonely-svc", lonely_iso)],
        "prod-kingdom2": [_dep("shared-app", group_iso)],
        "prod-kingdom3": [_dep("shared-app", group_iso)],
    }
    text = stats_digest.stale_deployments_section(
        db,
        ns_to_team={f"prod-kingdom{i}": f"kingdom{i}" for i in range(1, 4)},
        threshold_days=14,
        kubectl_fn=lambda ns: fake.get(ns, []),
        hide_expected=False,
    )
    assert "× 3 ns" in text          # группа свёрнута
    assert "shared-app" in text
    assert "lonely-svc" in text      # distinct-деплой НЕ выпал (был баг)


# ── M5a. Δ24h like-for-like (today-fires vs yesterday-fires) ────────────────


def _mk_metadata_db(*, yest_has, today_has, yest_rows, today_rows, chronic_rows, resurf_rows):
    """Порядок execute: yest_exists, today_exists, yest_rows, today_rows,
    chronic_rows, resurf_rows."""
    db = MagicMock()
    db.execute.side_effect = [
        MagicMock(scalar=lambda: yest_has),
        MagicMock(scalar=lambda: today_has),
        MagicMock(fetchall=lambda: yest_rows),
        MagicMock(fetchall=lambda: today_rows),
        MagicMock(fetchall=lambda: chronic_rows),
        MagicMock(fetchall=lambda: resurf_rows),
    ]
    return db


def test_delta24h_uses_today_minus_yesterday_not_firing_series():
    """`× 200` — это мгновенный firing-series count (VM). Δ24h НЕ должна
    равняться 200 − yesterday. Она должна быть today-fires(30) − yesterday(50)
    = −20, независимо от firing-series count."""
    counter = Counter({"KubePodCrashLooping": 200})
    db = _mk_metadata_db(
        yest_has=True, today_has=True,
        yest_rows=[("KubePodCrashLooping", 50)],
        today_rows=[("KubePodCrashLooping", 30)],
        chronic_rows=[], resurf_rows=[],
    )
    text = stats_digest.top_alert_types_section(counter, db)
    assert "× 200" in text            # firing-series count в заголовке строки
    assert "Δ24h -20" in text         # today 30 − yesterday 50, НЕ 200 − 50
    assert "Δ24h +150" not in text    # старая (баговая) формула cnt − yesterday


def test_delta24h_question_mark_when_today_window_absent():
    """Есть yesterday-history, но нет today-history (tracking только начался)
    → честный `Δ24h ?`, а не бессмысленная дельта."""
    counter = Counter({"KubePodCrashLooping": 200})
    db = _mk_metadata_db(
        yest_has=True, today_has=False,
        yest_rows=[("KubePodCrashLooping", 50)],
        today_rows=[],
        chronic_rows=[], resurf_rows=[],
    )
    text = stats_digest.top_alert_types_section(counter, db)
    assert "Δ24h ?" in text


# ── M5b. MTTR trend — предыдущее НЕпересекающееся окно ──────────────────────


def test_mttr_trend_compares_nonoverlapping_previous_window():
    """`_mttr_stats` для prev-окна должен вызываться с offset_days=days (то же
    окно длиной days, сдвинутое назад, без пересечения с current). Старый код
    звал days*2 без offset → 14-дневное супермножество."""
    calls = []

    def fake_mttr(db, days, offset_days=0):
        calls.append((days, offset_days))
        if offset_days == 0:
            return {"median_min": 10.0, "p95_min": 40.0, "samples": 100, "outliers_gt_7d": 0}
        return {"median_min": 15.0, "p95_min": 50.0, "samples": 80, "outliers_gt_7d": 0}

    with patch.object(stats_digest, "_mttr_stats", side_effect=fake_mttr):
        text = stats_digest.mttr_section(MagicMock(), days=7)

    assert (7, 0) in calls          # current window
    assert (7, 7) in calls          # prev — та же длина, без пересечения
    assert (14, 0) not in calls     # старое супермножество больше не запрашиваем
    # Trend рисуется даже когда у prev-окна МЕНЬШЕ samples (80 < 100) —
    # старый gate `prev.samples > now.samples` это ошибочно подавлял.
    assert "trend" in text
    assert "-5min" in text          # median 10 − 15
