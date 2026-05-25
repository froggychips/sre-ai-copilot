"""Тесты на stats_digest — pure data-aggregation, без LLM.

Покрываем рендеринг каждой секции на моках, плюс end-to-end build_digest
с in-memory state. Invariant-тест что модуль НЕ импортит LLM-классы —
в отдельном файле test_stats_digest_no_llm.py.
"""
from collections import Counter
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from app.services import stats_digest


# ── ns_to_team_map: business-team приоритетнее platform ─────────────────────

def test_ns_to_team_map_prefers_business_team_over_platform():
    """В KG в одном namespace могут быть и kingdom1 services, и platform
    (synthetic NATS-узлы). MIN()-filter не должен схватить platform."""
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [
        ("prod-kingdom1", "kingdom1"),
        ("prod-shared", "shared"),
        ("preprod-kingdom2", "kingdom2"),
    ]
    result = stats_digest._get_ns_to_team_map(db)
    assert result == {
        "prod-kingdom1": "kingdom1",
        "prod-shared": "shared",
        "preprod-kingdom2": "kingdom2",
    }


# ── cluster_health_section ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cluster_health_renders_with_data():
    vm = MagicMock()
    vm.get_cluster_health = AsyncMock(return_value=MagicMock(
        to_dict=lambda: {"nodes_ready": 16, "crashloops": 8, "firing_alerts": 47}
    ))
    text = await stats_digest.cluster_health_section(vm, fired_series=[{}] * 47)
    assert "Cluster Health" in text
    assert "`16`" in text  # nodes
    assert "`8`" in text   # crashloops
    assert "`47`" in text  # series count


@pytest.mark.asyncio
async def test_cluster_health_graceful_on_vm_error():
    vm = MagicMock()
    vm.get_cluster_health = AsyncMock(side_effect=RuntimeError("VM unreachable"))
    text = await stats_digest.cluster_health_section(vm, fired_series=[])
    assert "`?`" in text  # nodes fallback


# ── firing_alerts_section ──────────────────────────────────────────────────

def test_firing_alerts_groups_by_team_via_kg():
    fired = [
        {"metric": {"namespace": "prod-kingdom1", "alertname": "KubePodCrashLooping"}},
        {"metric": {"namespace": "prod-kingdom1", "alertname": "CPUThrottlingHigh"}},
        {"metric": {"namespace": "prod-shared", "alertname": "KubePodNotReady"}},
        {"metric": {"namespace": "monitoring", "alertname": "InfoInhibitor"}},  # unowned
    ]
    ns_to_team = {
        "prod-kingdom1": "kingdom1",
        "prod-shared": "shared",
        # monitoring → нет
    }
    text, unique, team_alerts, unowned = stats_digest.firing_alerts_section(fired, ns_to_team)
    assert "@kingdom1" in text
    assert "@shared" in text
    # Item #2: unowned теперь в отдельной секции, не inline.
    assert "monitoring=1" not in text
    assert unowned["monitoring"] == 1
    assert unique["KubePodCrashLooping"] == 1
    assert unique["CPUThrottlingHigh"] == 1
    assert team_alerts["kingdom1"] == 2
    assert team_alerts["shared"] == 1


def test_firing_alerts_empty_state_message():
    text, unique, team_alerts, unowned = stats_digest.firing_alerts_section([], {})
    assert "кластер здоров" in text


def test_firing_alerts_uses_english_series_unit_not_cyrillic_s():
    """Item #1 stats-UX: «@team 226с» читается как «секунды». Должно быть
    «series» (полное слово)."""
    fired = [
        {"metric": {"namespace": "prod-kingdom1", "alertname": "X"}}
        for _ in range(5)
    ]
    ns_to_team = {"prod-kingdom1": "kingdom1"}
    text, _, _, _ = stats_digest.firing_alerts_section(fired, ns_to_team)
    # Не должно быть «5с» (cyrillic с)
    assert "5с" not in text
    assert "series" in text
    assert "5 series" in text


def test_firing_alerts_squads_render_inline_single_line():
    """5+ teams ранее печатались каждая отдельной строкой (~7 строк),
    теперь должны быть в одной строке через запятую."""
    fired = [
        {"metric": {"namespace": f"prod-kingdom{i}", "alertname": "X"}}
        for i in range(1, 6)
        for _ in range(3)
    ]
    ns_to_team = {f"prod-kingdom{i}": f"kingdom{i}" for i in range(1, 6)}
    text, _, _, _ = stats_digest.firing_alerts_section(fired, ns_to_team)
    body_lines = [ln for ln in text.split("\n") if ln.strip().startswith("`@")]
    # Должна быть ОДНА body-строка с inline-перечислением teams,
    # не 5 отдельных.
    assert len(body_lines) == 1, f"Expected 1 inline line, got {len(body_lines)}: {body_lines}"
    assert "kingdom1" in body_lines[0]
    assert "kingdom5" in body_lines[0]


def test_stale_deployments_no_hidden_teaser():
    """Строка `… и ещё N (скрыто)` убрана как noise. Cap=6 + просто обрезать."""
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [("prod-kingdom1",)]
    now = datetime.now(timezone.utc)
    # 20 stale deployments → cap_total=6 покажет 6, остальные просто отброшены
    fakes = [
        _make_deployment(f"old-svc-{i}", (now - timedelta(days=60+i)).isoformat())
        for i in range(20)
    ]
    text = stats_digest.stale_deployments_section(
        db, ns_to_team={"prod-kingdom1": "kingdom1"}, threshold_days=14,
        kubectl_fn=lambda ns: fakes,
    )
    assert "скрыто" not in text
    assert "и ещё" not in text


# ── top_alert_types ────────────────────────────────────────────────────────

def test_top_alert_types_top_3():
    """Cap at top-3 (раньше было 5; уплотнили после Wave 2)."""
    counter = Counter({
        "KubePodCrashLooping": 30,
        "CPUThrottlingHigh": 25,  # noise — отфильтруется
        "KubeJobFailed": 8,
        "ScrapePoolHasNoTargets": 3,
        "FifthOne": 2,
        "SixthShouldBeHidden": 1,
    })
    text = stats_digest.top_alert_types_section(counter)
    assert "KubePodCrashLooping" in text
    assert "× 30" in text
    assert "SixthShouldBeHidden" not in text  # лимит 3


def test_top_alert_types_empty():
    text = stats_digest.top_alert_types_section(Counter())
    assert "нет активных алертов" in text


# ── fragile_services_section ───────────────────────────────────────────────

def test_fragile_services_renders_fragile_when_health_low_and_callers_high():
    """Item #6 stats-UX: «fragile» = health_score < 0.7 AND ≥3 callers.
    SQL row shape: (name, namespace, health_score, callers)."""
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [
        ("auth-service", "prod-shared", 0.18, 12),       # 🔴 fragile (low+high callers)
        ("town-service", "prod-kingdom1", 0.52, 5),      # 🟡 fragile
        ("dev-service", "prod-kingdom2", 0.81, 8),       # 🟢 healthy → blast-radius
    ]
    text = stats_digest.fragile_services_section(
        db, ns_to_team={"prod-shared": "shared", "prod-kingdom1": "kingdom1", "prod-kingdom2": "kingdom2"},
    )
    assert "Top fragile services" in text
    assert "auth-service" in text
    assert "health `0.18`" in text
    # healthy высокого-callers сервис идёт в blast-radius, не fragile
    assert "Top blast-radius services" in text
    assert "dev-service" in text


def test_fragile_services_health_ok_only_renders_blast_radius():
    """Если все сервисы здоровые (health ≥ 0.7) — только blast-radius секция,
    без «Top fragile»."""
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [
        ("payment-service", "prod-shared", 0.92, 25),
        ("town-service", "prod-kingdom1", 0.85, 12),
    ]
    text = stats_digest.fragile_services_section(
        db, ns_to_team={"prod-shared": "shared", "prod-kingdom1": "kingdom1"},
    )
    assert "Top fragile services" not in text
    assert "Top blast-radius services" in text
    assert "25 callers" in text


def test_fragile_services_blast_radius_only_when_health_not_computed():
    """Item #6: если health_score ни у кого не посчитан — только blast-radius
    с пометкой «health_score ещё не посчитан». Никаких «fragile» по
    inbound-degree (это была ошибка терминологии)."""
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [
        ("auth-service", "prod-shared", None, 5),
        ("town-service", "prod-kingdom1", None, 3),
    ]
    text = stats_digest.fragile_services_section(
        db, ns_to_team={"prod-shared": "shared", "prod-kingdom1": "kingdom1"},
    )
    assert "Top fragile services" not in text  # никаких inferred-fragile
    assert "Top blast-radius services" in text
    assert "health_score ещё не посчитан" in text
    assert "auth-service" in text
    assert "5 callers" in text


def test_fragile_services_empty_state_when_no_edges():
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []
    text = stats_digest.fragile_services_section(db, ns_to_team={})
    assert "нет edges" in text


# ── stale_deployments_section ──────────────────────────────────────────────

def _make_deployment(name: str, last_update_iso: str, replicas: int = 1) -> dict:
    return {
        "metadata": {
            "name": name,
            "annotations": {"meta.helm.sh/release-name": name},
            "creationTimestamp": last_update_iso,
        },
        "status": {
            "readyReplicas": replicas,
            "conditions": [{"lastUpdateTime": last_update_iso, "type": "Available", "status": "True"}],
        },
    }


def test_stale_deployments_groups_by_team_and_excludes_fresh():
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [("prod-kingdom1",), ("prod-kingdom2",)]

    now = datetime.now(timezone.utc)
    fake_deploys = {
        "prod-kingdom1": [
            _make_deployment("town-service", (now - timedelta(days=30)).isoformat()),
            _make_deployment("fresh-deploy", (now - timedelta(days=1)).isoformat()),
        ],
        "prod-kingdom2": [
            _make_deployment("legacy-app", (now - timedelta(days=90)).isoformat()),
        ],
    }
    text = stats_digest.stale_deployments_section(
        db,
        ns_to_team={"prod-kingdom1": "kingdom1", "prod-kingdom2": "kingdom2"},
        threshold_days=14,
        kubectl_fn=lambda ns: fake_deploys.get(ns, []),
    )
    assert "town-service" in text
    assert "legacy-app" in text  # application — попадает в suspicious
    assert "fresh-deploy" not in text  # под 14d threshold
    assert "@kingdom1" in text
    assert "@kingdom2" in text


def test_stale_deployments_squashes_cross_namespace_groups():
    """Same name в 3+ namespace-ах → одна squash-строка."""
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [
        ("prod-kingdom1",), ("prod-kingdom2",),
        ("prod-kingdom3",), ("prod-kingdom4",), ("prod-kingdom5",),
    ]
    now = datetime.now(timezone.utc)
    last_iso = (now - timedelta(days=62)).isoformat()
    fake_deploys = {
        f"prod-kingdom{i}": [_make_deployment("db-backup", last_iso)]
        for i in range(1, 6)
    }
    # Тест squash-логики: name содержит «backup» (expected), но мы явно
    # отключаем hide_expected чтобы протестировать сам group-by-name механизм.
    text = stats_digest.stale_deployments_section(
        db,
        ns_to_team={f"prod-kingdom{i}": f"kingdom{i}" for i in range(1, 6)},
        threshold_days=14,
        kubectl_fn=lambda ns: fake_deploys.get(ns, []),
        hide_expected=False,
    )
    # Один squash вместо 5 отдельных строк
    assert "× 5 ns" in text
    assert "db-backup" in text
    # idle отображается single number, не 5 раз
    assert text.count("62d") == 1


def test_stale_deployments_ignores_zero_replicas():
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [("prod-kingdom1",)]
    now = datetime.now(timezone.utc)
    fake = _make_deployment("scaled-zero", (now - timedelta(days=100)).isoformat(), replicas=0)
    text = stats_digest.stale_deployments_section(
        db, ns_to_team={"prod-kingdom1": "kingdom1"}, threshold_days=14,
        kubectl_fn=lambda ns: [fake],
    )
    assert "scaled-zero" not in text
    assert "ничего не stale" in text


# ── recent_deploys_section ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recent_deploys_renders_user_and_buildtype():
    now = datetime.now(timezone.utc)
    async def fake_fetch(*, lookback_hours, limit):
        return [
            {
                "id": 119313, "number": "488", "status": "SUCCESS",
                "branch": "refs/heads/preprod",
                "buildtype_name": "Build and update",
                "finished_at": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "triggered_by": "yaroslav.shulgin",
                "triggered_type": "user",
            },
            {
                "id": 119270, "number": "486", "status": "SUCCESS",
                "branch": "refs/heads/preprod",
                "buildtype_name": "Kingdom deploy",
                "finished_at": (now - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "triggered_by": None,
                "triggered_type": "vcs",  # auto-triggered, no user
            },
        ]
    text = await stats_digest.recent_deploys_section(fetch_fn=fake_fetch)
    assert "Recent deploys" in text
    assert "yaroslav.shulgin" in text
    assert "Build and update" in text
    assert "preprod #488" in text
    assert "2h ago" in text
    # auto-trigger без user — показываем type
    assert "_vcs_" in text or "vcs" in text


@pytest.mark.asyncio
async def test_recent_deploys_shows_status_marker_on_failure():
    async def fake_fetch(*, lookback_hours, limit):
        return [{
            "number": "500", "status": "FAILURE",
            "branch": "refs/heads/preprod",
            "buildtype_name": "Build and update",
            "finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "triggered_by": "user1", "triggered_type": "user",
        }]
    text = await stats_digest.recent_deploys_section(fetch_fn=fake_fetch)
    assert "FAILURE" in text


@pytest.mark.asyncio
async def test_recent_deploys_hides_section_when_tc_returns_nothing():
    """Если TC не сконфигурирован или нет deploy-билдов — секцию вообще
    скрываем (header без данных = шум). Возвращаем пустую строку."""
    async def fake_fetch(*, lookback_hours, limit):
        return []
    text = await stats_digest.recent_deploys_section(fetch_fn=fake_fetch)
    assert text == ""


@pytest.mark.asyncio
async def test_recent_deploys_graceful_on_fetch_error():
    async def boom(*, lookback_hours, limit):
        raise RuntimeError("TC down")
    text = await stats_digest.recent_deploys_section(fetch_fn=boom)
    assert text == ""  # пустая строка — секция скрывается без шума


# ── top_alert_types_section: noise-filter (Grok review compaction) ─────────

def test_top_alert_types_excludes_infrastructure_noise():
    """InfoInhibitor / Watchdog / CPUThrottlingHigh — служебные, не показываем."""
    counter = Counter({
        "InfoInhibitor": 250,         # noise
        "CPUThrottlingHigh": 175,     # noise
        "Watchdog": 50,               # noise
        "KubePodCrashLooping": 10,    # real
        "etcdInsufficientMembers": 8,  # real
    })
    text = stats_digest.top_alert_types_section(counter)
    assert "InfoInhibitor" not in text
    assert "CPUThrottlingHigh" not in text
    assert "Watchdog" not in text
    assert "KubePodCrashLooping" in text
    assert "etcdInsufficientMembers" in text


# ── anomaly_summary_section (Wave 2) ───────────────────────────────────────


def test_anomaly_summary_empty_zero_anomalies():
    """Таблица есть, но за 24h ничего не нашлось — показываем «всё в норме»."""
    db = MagicMock()
    db.execute.return_value.fetchone.return_value = (0, 0)
    text = stats_digest.anomaly_summary_section(db)
    assert "Anomalies" in text
    assert "ни одной аномалии" in text


def test_anomaly_summary_missing_table_returns_empty_string():
    """На dev без миграции — try/except → секция полностью скрыта."""
    db = MagicMock()
    db.execute.side_effect = RuntimeError("relation kg_anomaly_observations does not exist")
    text = stats_digest.anomaly_summary_section(db)
    assert text == ""


def test_anomaly_summary_renders_total_severity_top_metric():
    db = MagicMock()
    # Порядок SQL-вызовов: total → by_severity → top_services → by_metric
    db.execute.side_effect = [
        MagicMock(fetchone=lambda: (47, 12)),  # total, distinct_services
        MagicMock(fetchall=lambda: [("warning", 40), ("critical", 7)]),
        MagicMock(fetchall=lambda: [
            ("mv-service", 12),
            ("town-grainhost", 8),
            ("auth-service", 5),
        ]),
        MagicMock(fetchall=lambda: [
            ("p95_latency_ms", 20),
            ("http_5xx_rate", 15),
            ("restarts_rate", 12),
        ]),
    ]
    text = stats_digest.anomaly_summary_section(db)
    assert "Total: 47" in text
    assert "12 svc" in text
    assert "warning: 40" in text
    assert "critical: 7" in text
    assert "`mv-service` ×12" in text
    assert "p95×20" in text
    assert "5xx×15" in text


# ── anomaly_top_section (Wave 2) ───────────────────────────────────────────


def test_anomaly_top_renders_persistent_pairs():
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [
        ("mv-service", "prod-shared", "p95_latency_ms", 12, 4.2),
        ("town-grainhost", "prod-kingdom1", "restarts_rate", 8, 3.7),
    ]
    text = stats_digest.anomaly_top_section(
        db, ns_to_team={"prod-shared": "shared", "prod-kingdom1": "kingdom1"},
    )
    assert "Persistent anomalies" in text
    assert "`mv-service`" in text
    assert "p95" in text
    assert "12 events" in text
    assert "z=4.2" in text
    assert "@shared" in text
    assert "@kingdom1" in text


def test_anomaly_top_hidden_when_empty():
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []
    text = stats_digest.anomaly_top_section(db, ns_to_team={})
    assert text == ""


def test_anomaly_top_hidden_when_table_missing():
    db = MagicMock()
    db.execute.side_effect = RuntimeError("table does not exist")
    text = stats_digest.anomaly_top_section(db, ns_to_team={})
    assert text == ""


# ── log_errors_section (Wave 2) ────────────────────────────────────────────


def test_log_errors_renders_top_3_with_sample():
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [
        ("mv-service", "prod-shared", 234, "connection refused: 10.0.5.13"),
        ("town-grainhost", "prod-kingdom1", 87, "NullReferenceException at TownActor.OnTick"),
    ]
    text = stats_digest.log_errors_section(
        db, ns_to_team={"prod-shared": "shared", "prod-kingdom1": "kingdom1"},
    )
    assert "Log errors" in text
    assert "`mv-service`" in text
    assert "234 errors" in text
    assert "connection refused" in text
    assert "@shared" in text


def test_log_errors_hidden_when_empty():
    """Таблица пуста (Seq env-vars не сконфигурены) — секция скрыта."""
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []
    text = stats_digest.log_errors_section(db, ns_to_team={})
    assert text == ""


def test_log_errors_hidden_when_table_missing():
    db = MagicMock()
    db.execute.side_effect = RuntimeError("relation kg_log_observations does not exist")
    text = stats_digest.log_errors_section(db, ns_to_team={})
    assert text == ""


def test_log_errors_sample_truncated_to_60_chars():
    db = MagicMock()
    long_msg = "A" * 200
    db.execute.return_value.fetchall.return_value = [
        ("svc", "prod-ns", 10, long_msg),
    ]
    text = stats_digest.log_errors_section(db, ns_to_team={"prod-ns": "team"})
    assert "..." in text
    # Не должно быть полной 200-char строки
    assert "A" * 200 not in text


# ── cluster_health trend (Wave 2) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_cluster_health_renders_trend_when_history_available():
    vm = MagicMock()
    vm.get_cluster_health = AsyncMock(return_value=MagicMock(
        to_dict=lambda: {"nodes_ready": 16, "crashloops": 5}
    ))
    db = MagicMock()
    # today (24h) — 5 столбцов: cpu, mem, disk, crash, count
    today = (34.2, 37.1, 47.5, 5.0, 144)
    yesterday = (31.0, 36.0, 45.0, 3.0, 144)
    db.execute.side_effect = [
        MagicMock(fetchone=lambda: today),
        MagicMock(fetchone=lambda: yesterday),
    ]
    text = await stats_digest.cluster_health_section(vm, fired_series=[], db=db)
    assert "Trend" in text
    assert "31→34%" in text  # cpu rounded
    assert "+3pp" in text
    assert "crashloops avg 3→5" in text
    assert "+2" in text


@pytest.mark.asyncio
async def test_cluster_health_fallback_when_no_yesterday_data():
    """< 48h истории — рисуем snapshot + пометку «недостаточно данных»."""
    vm = MagicMock()
    vm.get_cluster_health = AsyncMock(return_value=MagicMock(
        to_dict=lambda: {"nodes_ready": 16, "crashloops": 5}
    ))
    db = MagicMock()
    today = (34.2, 37.1, 47.5, 5.0, 144)
    yesterday = (None, None, None, None, 0)  # пусто
    db.execute.side_effect = [
        MagicMock(fetchone=lambda: today),
        MagicMock(fetchone=lambda: yesterday),
    ]
    text = await stats_digest.cluster_health_section(vm, fired_series=[], db=db)
    assert "недостаточно данных" in text
    assert "Trend" not in text


@pytest.mark.asyncio
async def test_cluster_health_no_db_omits_trend_section():
    """Старая signature без db работает (для unit-тестов без БД)."""
    vm = MagicMock()
    vm.get_cluster_health = AsyncMock(return_value=MagicMock(
        to_dict=lambda: {"nodes_ready": 16, "crashloops": 8}
    ))
    text = await stats_digest.cluster_health_section(vm, fired_series=[{}] * 47)
    assert "недостаточно данных" in text
    assert "Trend" not in text


# ── kg_quality_section ─────────────────────────────────────────────────────

def test_kg_quality_renders_full_state():
    """Order of execute-вызовов в kg_quality_section (compact, без Team-owned):
       1. services_total .scalar
       2. edges_total .scalar
       3. edges_by_kind .fetchall
       4. synthetic .scalar
       5. orphan (исключая synthetic) .scalar
    """
    db = MagicMock()
    call_results = [
        MagicMock(scalar=lambda: 384),
        MagicMock(scalar=lambda: 696),
        MagicMock(fetchall=lambda: [("calls", 36), ("uses_nats", 660)]),
        MagicMock(scalar=lambda: 82),   # synthetic
        MagicMock(scalar=lambda: 47),   # real-orphan
    ]
    db.execute.side_effect = call_results

    rendered = stats_digest.kg_quality_section(db)
    assert "`384`" in rendered
    assert "`47`/`302`" in rendered  # orphan / (total - synthetic)
    assert "(15%)" in rendered  # 47/302 = 15.56% → 15
    assert "synthetic скрыты: `82`" in rendered
    assert "calls=36" in rendered
    assert "uses_nats=660" in rendered
    # Team-owned строка убрана (всегда 100% после KG team_owner enrichment) —
    # она была шумом, занимала самую длинную строку в digest.
    assert "Team-owned" not in rendered


def test_kg_quality_no_synthetic_no_suffix():
    """Если synthetic=0 — не показывать пустую подпись."""
    db = MagicMock()
    db.execute.side_effect = [
        MagicMock(scalar=lambda: 100),
        MagicMock(scalar=lambda: 200),
        MagicMock(fetchall=lambda: [("calls", 200)]),
        MagicMock(scalar=lambda: 0),   # synthetic = 0
        MagicMock(scalar=lambda: 10),
    ]
    rendered = stats_digest.kg_quality_section(db)
    assert "synthetic скрыты" not in rendered
    assert "`10`/`100`" in rendered


def test_kg_quality_empty_kg():
    db = MagicMock()
    db.execute.return_value.scalar.return_value = 0
    text = stats_digest.kg_quality_section(db)
    assert "KG пустой" in text


# ── _last_update fallback ──────────────────────────────────────────────────

def test_last_update_from_conditions():
    dep = {
        "metadata": {"creationTimestamp": "2026-01-01T00:00:00Z"},
        "status": {"conditions": [
            {"lastUpdateTime": "2026-04-01T12:00:00Z"},
            {"lastUpdateTime": "2026-05-01T12:00:00Z"},  # max
        ]},
    }
    dt = stats_digest._last_update(dep)
    assert dt is not None
    assert dt.month == 5  # max выбран


def test_last_update_fallback_creation_timestamp():
    dep = {
        "metadata": {"creationTimestamp": "2026-01-01T00:00:00Z"},
        "status": {"conditions": []},
    }
    dt = stats_digest._last_update(dep)
    assert dt is not None
    assert dt.year == 2026 and dt.month == 1


def test_last_update_returns_none_when_no_data():
    assert stats_digest._last_update({"metadata": {}, "status": {}}) is None


# ── send_daily_digest gate ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_daily_digest_skips_when_disabled():
    with patch.object(stats_digest.settings, "STATS_DIGEST_ENABLED", False):
        result = await stats_digest.send_daily_digest(db=MagicMock())
    assert result == {"status": "skipped", "reason": "disabled"}


@pytest.mark.asyncio
async def test_send_daily_digest_sends_when_enabled():
    fake_discord = MagicMock()
    fake_discord.send_stats_report = AsyncMock()
    # Overhaul: send_daily_digest зовёт _build_digest_with_meta напрямую,
    # build_digest остался как thin wrapper. Мокаем внутренний помощник,
    # отключаем skip-if-noop чтобы тест был детерминистичен.
    fake_meta = {
        "sections_with_content": 5,
        "change_report": stats_digest.ChangeReport(new_alerts_24h=3),
        "fired_series_count": 10,
    }
    with patch.object(stats_digest.settings, "STATS_DIGEST_ENABLED", True), \
         patch.object(stats_digest.settings, "VICTORIA_METRICS_URL", ""), \
         patch.object(stats_digest.settings, "STATS_DIGEST_SKIP_NOOP", False, create=True), \
         patch.object(
             stats_digest, "_build_digest_with_meta",
             new=AsyncMock(return_value=("DIGEST_BODY", fake_meta)),
         ), \
         patch("app.services.discord_service.discord_service", fake_discord):
        result = await stats_digest.send_daily_digest(db=MagicMock())
    assert result["status"] == "sent"
    fake_discord.send_stats_report.assert_awaited_once_with("DIGEST_BODY")
