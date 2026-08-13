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

Ревью-фиксы 2026-08-10:
  * R1 — пустой VICTORIA_METRICS_URL не роняет сборку NameError'ом
    (vm=None + mcp_kg_usage_section скрывается без VM).
  * R2 — chronic action items группируются с namespace: одноимённые сервисы
    из разных ns не схлопываются в один ложный chronic (хвост #254).
  * R3 — deadman-heartbeat доставки пишется ТОЛЬКО при подтверждённой
    отправке (send_stats_report → bool); False → status=send_failed.

Все тесты на MagicMock — без живого PostgreSQL. Для M2/M4/R2 PG-специфичный
SQL не гоняется, поэтому проверяем сам SQL-текст и поведение на моках.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import stats_digest
from app.services.digest import failures as digest_failures


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


# ── R1. VM-less сборка: пустой VICTORIA_METRICS_URL не роняет дайджест ──────


def _empty_db() -> MagicMock:
    """Session-мок, отвечающий «пусто» на любой запрос секции."""
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []
    db.execute.return_value.fetchone.return_value = None
    db.execute.return_value.scalar.return_value = 0
    return db


@pytest.mark.asyncio
async def test_build_digest_survives_empty_victoria_metrics_url():
    """Регрессия: `vm` рождался только внутри `if settings.VICTORIA_METRICS_URL`,
    а `mcp_kg_usage_section(vm)` звался безусловно — при пустом URL вся сборка
    падала NameError, и дайджест не выходил вообще.

    VM-less путь легитимен: секции без VM обязаны честно сказать «не настроен»
    (Cluster Health) либо скрыться (MCP usage), а сборка — дойти до конца.
    """
    db = _empty_db()
    with patch.object(stats_digest.settings, "VICTORIA_METRICS_URL", ""), \
         patch.object(stats_digest, "_read_last_firing_series",
                      new=AsyncMock(return_value=None)), \
         patch.object(stats_digest, "_read_day_snapshot",
                      new=AsyncMock(return_value=None)), \
         patch.object(stats_digest, "_write_last_firing_series",
                      new=AsyncMock(return_value=None)), \
         patch.object(stats_digest, "_write_day_snapshot",
                      new=AsyncMock(return_value=None)), \
         patch.object(stats_digest, "recent_deploys_section",
                      new=AsyncMock(return_value="")), \
         patch.object(stats_digest, "_kubectl_get_deployments_json",
                      return_value=[]), \
         patch.object(stats_digest, "pipeline_health_section", return_value=""), \
         patch.object(stats_digest, "beat_heartbeats_footer", return_value=""):
        content, meta = await stats_digest._build_digest_with_meta(db)

    assert "Cluster Daily Digest" in content
    # Соседняя секция честно объявляет об отсутствии VM…
    assert "VICTORIA_METRICS_URL не настроен" in content
    # …а MCP-usage без VM просто скрыт, а не падает.
    assert "KG через MCP" not in content
    assert "mcp_kg_usage_section" not in (meta["failed_sections"] or [])


@pytest.mark.asyncio
async def test_mcp_kg_usage_section_hidden_without_vm():
    """vm=None → секция скрыта и НЕ считается упавшей (это штатный путь,
    а не сбой: жаловаться на ненастроенный VM — работа Cluster Health)."""
    stats_digest._reset_section_failures()
    assert await stats_digest.mcp_kg_usage_section(None) == ""
    try:
        failures = digest_failures.failed_sections()
    except LookupError:
        failures = []
    assert failures == []


# ── R2. Chronic action items: namespace в GROUP BY и в строке ───────────────


def test_chronic_action_items_group_by_includes_namespace():
    """Хвост #254: без ns в GROUP BY семь одноимённых `bot` из разных ns по
    2 fires схлопывались в один ложный chronic «14 fires»."""
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []
    stats_digest._chronic_action_items(db, threshold=10)

    sql = str(db.execute.call_args[0][0])
    assert "s.namespace" in sql
    assert "GROUP BY s.id, s.name, s.namespace, a.alertname" in sql


def test_chronic_action_items_keeps_same_name_from_different_namespaces():
    """Одноимённые сервисы из разных ns — ДВА разных item-а с своими fires."""
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [
        ("bot", "prod-kingdom1", "KubePodCrashLooping", 12),
        ("bot", "prod-kingdom2", "KubePodCrashLooping", 11),
    ]
    items = stats_digest._chronic_action_items(db, threshold=10)
    assert [(i["service"], i["namespace"], i["fires"]) for i in items] == [
        ("bot", "prod-kingdom1", 12),
        ("bot", "prod-kingdom2", 11),
    ]


def test_action_items_renders_namespace_next_to_chronic_service():
    """В строке дайджеста ns стоит рядом с именем — иначе два `bot` в
    «RCA: bot, bot» неразличимы."""
    db = MagicMock()
    with patch.object(stats_digest, "_chronic_action_items", return_value=[
        {"service": "bot", "namespace": "prod-kingdom1",
         "alertname": "KubePodCrashLooping", "fires": 12},
        {"service": "bot", "namespace": "prod-kingdom2",
         "alertname": "KubePodCrashLooping", "fires": 11},
    ]), \
         patch.object(stats_digest, "_unowned_action_items", return_value=0), \
         patch.object(stats_digest, "_suspicious_stale_action_items", return_value=0):
        text = stats_digest.action_items_section(db)

    assert "`bot` prod-kingdom1" in text
    assert "`bot` prod-kingdom2" in text


def test_suspicious_in_prod_top_renders_with_namespace():
    """Топ-имена prod-bucket-а тоже с ns (были только имена)."""
    db = MagicMock()
    with patch.object(stats_digest, "_chronic_action_items", return_value=[]), \
         patch.object(stats_digest, "_unowned_action_items", return_value=0), \
         patch.object(stats_digest, "_suspicious_stale_action_items", return_value=5), \
         patch.object(stats_digest, "_suspicious_in_prod_with_alerts",
                      return_value=(2, [("bot", "prod-kingdom1"),
                                        ("bot", "prod-kingdom2")])), \
         patch.object(stats_digest, "_suspicious_with_callers", return_value=0), \
         patch.object(stats_digest, "_suspicious_in_external_or_mcp", return_value=0):
        text = stats_digest.action_items_section(db)

    assert "`bot` prod-kingdom1" in text
    assert "`bot` prod-kingdom2" in text


def test_suspicious_in_prod_with_alerts_selects_namespace():
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []
    stats_digest._suspicious_in_prod_with_alerts(db, days=60)
    sql = str(db.execute.call_args[0][0])
    assert "SELECT s.name, s.namespace" in sql


# ── R3. Deadman-маркер доставки только при подтверждённой отправке ──────────


def _sent_meta() -> dict:
    return {
        "sections_with_content": 3,
        "change_report": stats_digest.ChangeReport(new_alerts_24h=1),
        "fired_series_count": 1,
    }


@pytest.mark.asyncio
async def test_heartbeat_not_recorded_when_delivery_failed():
    """send_stats_report глотает свои ошибки и возвращает False (нет вебхука /
    HTTP>=400). Раньше heartbeat писался безусловно — deadman видел «доставлено»
    у дайджеста, который до Discord не доехал, и молчал."""
    fake_discord = MagicMock()
    fake_discord.send_stats_report = AsyncMock(return_value=False)
    heartbeat = MagicMock()
    with patch.object(stats_digest.settings, "STATS_DIGEST_ENABLED", True), \
         patch.object(stats_digest.settings, "STATS_DIGEST_SKIP_NOOP", False, create=True), \
         patch.object(stats_digest, "_build_digest_with_meta",
                      new=AsyncMock(return_value=("BODY", _sent_meta()))), \
         patch.object(stats_digest, "_record_task_heartbeat", heartbeat), \
         patch("app.services.discord_service.discord_service", fake_discord):
        result = await stats_digest.send_daily_digest(db=MagicMock())

    assert result["status"] == "send_failed"
    heartbeat.assert_not_called()


@pytest.mark.asyncio
async def test_heartbeat_recorded_when_delivery_confirmed():
    """True (2xx или DISCORD_DRY_RUN) → heartbeat доставки пишется."""
    fake_discord = MagicMock()
    fake_discord.send_stats_report = AsyncMock(return_value=True)
    heartbeat = MagicMock()
    with patch.object(stats_digest.settings, "STATS_DIGEST_ENABLED", True), \
         patch.object(stats_digest.settings, "STATS_DIGEST_SKIP_NOOP", False, create=True), \
         patch.object(stats_digest, "_build_digest_with_meta",
                      new=AsyncMock(return_value=("BODY", _sent_meta()))), \
         patch.object(stats_digest, "_record_task_heartbeat", heartbeat), \
         patch("app.services.discord_service.discord_service", fake_discord):
        result = await stats_digest.send_daily_digest(db=MagicMock())

    assert result["status"] == "sent"
    heartbeat.assert_called_once_with(stats_digest.DIGEST_DELIVERY_TASK)
