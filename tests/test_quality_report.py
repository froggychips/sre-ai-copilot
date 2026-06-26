"""Тесты на ``app.scripts.quality_report``.

In-memory SQLite + ORM-фикстуры: поднимаем minimal KG и проверяем
числа в QualityReport + рендер (markdown/JSON).

Сценарии:
  * пустая БД → корректные null/zero без падений
  * 3 real + 1 synthetic — ownership math
  * edges by kind — корректные суммы и счёт orphan
  * jobs linkage — denominator/pct
  * stale_class breakdown + top-10
  * alerts 24h окно — owner/blast/nats/pod_trail
  * deploys 30d — sha + linkage
  * JSON output — schema (все ключи присутствуют)
  * markdown output — все обязательные секции
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.schema import (AlertEvent, Deployment, K8sJob,
                                        PodEvent, Service, ServiceEdge,
                                        StorageVolume, VolumeEdge)
from app.knowledge_graph.stale_classifier import (STALE_CLASS_ACTIVE,
                                                  STALE_CLASS_EXPECTED,
                                                  STALE_CLASS_SUSPICIOUS)
from app.scripts.quality_report import (OWNER_SOURCE_UNTRACKED, CheckThresholds,
                                        QualityReport, _coerce_metadata_dict,
                                        build_report, evaluate_check,
                                        render_json, render_markdown)


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _mk_svc(
    db,
    *,
    name: str,
    namespace: str = "ns",
    team_owner=None,
    synthetic: bool = False,
    metadata_json=None,
    stale_class=None,
) -> Service:
    s = Service(
        name=name,
        namespace=namespace,
        team_owner=team_owner,
        metadata_json=metadata_json,
    )
    s.synthetic = synthetic
    s.stale_class = stale_class
    db.add(s)
    db.flush()
    return s


def _mk_edge(db, src_id: int, dst_id: int, kind: str, *, extras=None) -> ServiceEdge:
    e = ServiceEdge(src_id=src_id, dst_id=dst_id, kind=kind, weight=1, extras=extras)
    db.add(e)
    db.flush()
    return e


# ── empty DB ─────────────────────────────────────────────────────────────────


def test_empty_db_returns_report_with_zeros(db):
    """Пустая БД — все count = 0, все pct = None, без exceptions."""
    report = build_report(db)
    assert isinstance(report, QualityReport)
    assert report.services_total_real == 0
    assert report.services_total_synthetic == 0
    assert report.owner_known_count == 0
    assert report.owner_known_pct is None
    assert report.owner_sources == {}
    assert report.edges_by_kind == {}
    assert report.jobs_total == 0
    assert report.alerts_24h_total == 0
    assert report.deploys_30d_total == 0
    assert report.stale_active == 0


# ── ownership math ──────────────────────────────────────────────────────────


def test_ownership_real_vs_synthetic_split(db):
    """3 real + 1 synthetic-by-flag + 1 synthetic-by-prefix → real=3, synth=2."""
    _mk_svc(db, name="a", team_owner="squad-1")
    _mk_svc(db, name="b", team_owner="squad-2")
    _mk_svc(db, name="c", team_owner=None)
    _mk_svc(db, name="syn-flag", synthetic=True, team_owner="platform")
    _mk_svc(db, name="ingress:foo.com")  # synthetic by name
    db.commit()

    r = build_report(db)
    assert r.services_total_real == 3
    assert r.services_total_synthetic == 2


def test_owner_known_pct_excludes_unknown_marker(db):
    """team_owner='unknown' не считается известным (см. owner_known utility)."""
    _mk_svc(db, name="a", team_owner="squad-1")
    _mk_svc(db, name="b", team_owner="")
    _mk_svc(db, name="c", team_owner="unknown")
    _mk_svc(db, name="d", team_owner=None)
    _mk_svc(db, name="e", team_owner="squad-2")
    db.commit()

    r = build_report(db)
    assert r.services_total_real == 5
    assert r.owner_known_count == 2  # squad-1 + squad-2
    assert r.owner_known_pct == 40.00


def test_owner_sources_breakdown_from_metadata(db):
    """metadata_json.owner_source → breakdown по каноническим source-ам.

    Регрессионный тест против бага "owner_source: namespace_prefix → 21
    при 1994 реально проставленных owners": сумма всех бакетов должна
    совпадать с owner_known_count, иначе breakdown врёт.
    """
    _mk_svc(db, name="a", team_owner="squad-1",
            metadata_json={"owner_source": "namespace_prefix"})
    _mk_svc(db, name="b", team_owner="squad-2",
            metadata_json={"owner_source": "namespace_prefix"})
    _mk_svc(db, name="c", team_owner="platform",
            metadata_json={"owner_source": "k8s_labels"})
    _mk_svc(db, name="d", team_owner="data")  # no metadata
    _mk_svc(db, name="e", team_owner="kingdom1",
            metadata_json={"owner_source": "deploy_history"})
    db.commit()

    r = build_report(db)
    assert r.owner_sources == {
        "namespace_prefix": 2,
        "k8s_labels": 1,
        "deploy_history": 1,
        OWNER_SOURCE_UNTRACKED: 1,  # svc "d" — owner есть, source нет
    }
    # Invariant: сумма breakdown == owner_known_count
    assert sum(r.owner_sources.values()) == r.owner_known_count == 5


def test_owner_sources_breakdown_counts_inferred_without_source(db):
    """Главный регрессионный кейс: topology_sync пишет team_owner через
    namespace-prefix эвристику без owner_source маркера. До PR #97 такие
    сервисы выпадали из breakdown — отчёт показывал "21 namespace_prefix"
    при ~2000 реально owned-сервисов. После PR — они в bucket
    ``inferred_no_source``.
    """
    # 22 services with explicit namespace_prefix (как backfill_ownership пишет)
    for i in range(22):
        _mk_svc(db, name=f"explicit-{i}", namespace=f"squad-{i}",
                team_owner=f"squad-{i}",
                metadata_json={"owner_source": "namespace_prefix"})
    # 100 services где topology_sync проставил owner без source-маркера
    for i in range(100):
        _mk_svc(db, name=f"inferred-{i}", namespace=f"squad-{i % 10}",
                team_owner=f"squad-{i % 10}")
    db.commit()

    r = build_report(db)
    assert r.owner_known_count == 122
    assert r.owner_sources["namespace_prefix"] == 22
    assert r.owner_sources[OWNER_SOURCE_UNTRACKED] == 100
    # Invariant
    assert sum(r.owner_sources.values()) == r.owner_known_count


def test_owner_sources_excludes_unknown_marker_from_breakdown(db):
    """team_owner='unknown' / 'n/a' / '-' / 'none' не считается owned —
    значит и в breakdown не попадает (тот же фильтр что и в owner_known).
    """
    _mk_svc(db, name="a", team_owner="squad-1",
            metadata_json={"owner_source": "namespace_prefix"})
    _mk_svc(db, name="b", team_owner="unknown",
            metadata_json={"owner_source": "namespace_prefix"})
    _mk_svc(db, name="c", team_owner="n/a")
    _mk_svc(db, name="d", team_owner="-")
    db.commit()

    r = build_report(db)
    assert r.owner_known_count == 1  # только squad-1
    assert r.owner_sources == {"namespace_prefix": 1}
    assert sum(r.owner_sources.values()) == r.owner_known_count


def test_owner_sources_note_when_empty(db):
    """Без сервисов с team_owner — owner_sources пустой, note об этом."""
    _mk_svc(db, name="a")  # no owner at all
    db.commit()
    r = build_report(db)
    assert r.owner_sources == {}
    assert "пуст" in r.owner_sources_note


def test_owner_sources_note_when_all_inferred(db):
    """Все owners проставлены без source-маркера → note об этом."""
    _mk_svc(db, name="a", team_owner="squad-1")
    _mk_svc(db, name="b", team_owner="squad-2")
    db.commit()
    r = build_report(db)
    assert r.owner_sources == {OWNER_SOURCE_UNTRACKED: 2}
    assert "без явного" in r.owner_sources_note


def test_owner_sources_handles_metadata_json_as_string(db):
    """Defensive: если metadata_json пришёл строкой (legacy migration на
    TEXT-колонку) — должны распарсить, а не молча отбросить.
    """
    # SQLAlchemy JSON-колонка на SQLite де-факто хранит как text — но
    # ORM-сериализатор при assign-е dict проворачивает json.dumps. Чтобы
    # сэмулировать "пришла строка" — bypass через прямую установку
    # сырого значения через executemany не работает (SA всё равно пройдёт
    # десериализацию). Поэтому юнит-тестируем _coerce_metadata_dict напрямую.
    assert _coerce_metadata_dict(
        '{"owner_source": "namespace_prefix"}'
    ) == {"owner_source": "namespace_prefix"}
    assert _coerce_metadata_dict({"owner_source": "labels"}) == {"owner_source": "labels"}
    assert _coerce_metadata_dict(None) is None
    assert _coerce_metadata_dict("not-json") is None
    assert _coerce_metadata_dict("[1,2,3]") is None  # JSON но не dict
    assert _coerce_metadata_dict(b'{"owner_source": "manual"}') == {"owner_source": "manual"}


# ── topology ─────────────────────────────────────────────────────────────────


def test_edges_by_kind_aggregates(db):
    a = _mk_svc(db, name="a")
    b = _mk_svc(db, name="b")
    c = _mk_svc(db, name="c")
    _mk_edge(db, a.id, b.id, "calls")
    _mk_edge(db, b.id, c.id, "calls")
    _mk_edge(db, a.id, c.id, "uses_db")
    _mk_edge(db, c.id, a.id, "uses_nats")
    db.commit()

    r = build_report(db)
    assert r.edges_by_kind == {"calls": 2, "uses_db": 1, "uses_nats": 1}


def test_orphan_by_http_and_nats(db):
    """3 real-сервиса. a-->b (calls); c — orphan по HTTP. a — uses_nats c.
    HTTP-orphans = {c}; NATS-orphans = {b}.
    """
    a = _mk_svc(db, name="a")
    b = _mk_svc(db, name="b")
    c = _mk_svc(db, name="c")
    _mk_edge(db, a.id, b.id, "calls")
    _mk_edge(db, a.id, c.id, "uses_nats")
    db.commit()

    r = build_report(db)
    assert r.services_without_http_edges == 1  # c
    assert r.services_without_nats_edges == 1  # b
    # app-orphan (любой meaningful edge calls/routes_to/uses_db/uses_nats):
    # a,b связаны через calls; c — через uses_nats. Все три connected → 0.
    assert r.services_orphan_app == 0
    assert r.services_app_scope_total == 3


def test_orphan_app_nats_only_not_orphan(db):
    """Сервис, связанный ТОЛЬКО через uses_nats, не считается app-orphan.

    WO-сервисы общаются в основном через NATS/Orleans, а не HTTP REST —
    HTTP-only метрика давала ложные orphan-ы (issue #2).
    """
    a = _mk_svc(db, name="a")  # HTTP src
    b = _mk_svc(db, name="b")  # связан только через uses_nats
    c = _mk_svc(db, name="c")  # связан только через uses_db
    _mk_svc(db, name="isolated")  # вообще без edges → orphan
    _mk_edge(db, a.id, a.id, "calls")  # a имеет HTTP (self-loop держит http-метрику)
    _mk_edge(db, a.id, b.id, "uses_nats")
    _mk_edge(db, c.id, a.id, "uses_db")  # cross-node: c связан с a через uses_db
    db.commit()

    r = build_report(db)
    # b (uses_nats) и c (uses_db) connected; orphan только isolated.
    assert r.services_orphan_app == 1  # isolated
    assert r.services_app_scope_total == 4
    # но HTTP-only метрика по-прежнему считает b/c/isolated orphan-ами:
    assert r.services_without_http_edges == 3  # b, c, isolated


def test_orphan_app_excludes_expected_stale_infra(db):
    """expected_stale-инфра (DB/headless/system) исключена из знаменателя.

    Такие сервисы edge-less by design и не должны валить gate.
    """
    app_ok = _mk_svc(db, name="app-ok", stale_class=STALE_CLASS_ACTIVE)
    _mk_svc(db, name="app-orphan", stale_class=STALE_CLASS_SUSPICIOUS)
    # инфра без edges — не должна попасть ни в orphan, ни в знаменатель:
    infra_db = _mk_svc(db, name="infra-db", stale_class=STALE_CLASS_EXPECTED)
    # cross-node ребро (app_ok → infra-db): app_ok связан, значит non-orphan.
    _mk_edge(db, app_ok.id, infra_db.id, "uses_nats")
    db.commit()

    r = build_report(db)
    # scope = {app-ok, app-orphan}; infra-db исключён.
    assert r.services_app_scope_total == 2
    assert r.services_orphan_app == 1  # только app-orphan

    # gate: 1/2 = 50% > 10% дефолт (contract.orphan_rate_max_pct) → fail;
    # axis считается по app-метрике.
    result = evaluate_check(r, db, CheckThresholds())
    assert result.orphan_pct == 50.0
    orphan_fail = [f for f in result.failures if f["axis"] == "orphan_pct"]
    assert orphan_fail and "expected_stale" in orphan_fail[0]["detail"]

    # А если поднять порог — orphan-axis проходит (инфра не мешает).
    result_relaxed = evaluate_check(r, db, CheckThresholds(max_orphan_pct=60.0))
    assert not [f for f in result_relaxed.failures if f["axis"] == "orphan_pct"]


def test_jobs_linkage_pct(db):
    """5 jobs total, 3 linked → 60%."""
    a = _mk_svc(db, name="a", namespace="ns1")
    _ = a  # services existence не используется напрямую в metric
    for i, owner in enumerate(["svc-a", "svc-b", "svc-c", None, None]):
        j = K8sJob(
            namespace="ns1",
            name=f"job-{i}",
            kind="cronjob",
            owner_service_name=owner,
        )
        db.add(j)
    db.commit()

    r = build_report(db)
    assert r.jobs_total == 5
    assert r.jobs_linked_to_service == 3
    assert r.jobs_linked_pct == 60.00


def test_volume_edges_and_storage_kinds(db):
    db.add(StorageVolume(kind="pvc", namespace="ns", name="data-pg-0"))
    db.add(StorageVolume(kind="pv", namespace="", name="pvc-abc"))
    db.add(StorageVolume(kind="pvc", namespace="ns", name="data-pg-1"))
    db.flush()
    db.add(VolumeEdge(src_kind="service", src_id=1, dst_kind="pvc", dst_id=1, kind="uses_volume"))
    db.add(VolumeEdge(src_kind="pvc", src_id=1, dst_kind="pv", dst_id=1, kind="bound_to"))
    db.add(VolumeEdge(src_kind="pvc", src_id=2, dst_kind="pv", dst_id=1, kind="bound_to"))
    db.commit()

    r = build_report(db)
    assert r.storage_volumes_by_kind == {"pvc": 2, "pv": 1}
    assert r.volume_edges_by_kind == {"uses_volume": 1, "bound_to": 2}


# ── stale classification ─────────────────────────────────────────────────────


def test_stale_breakdown_and_top_suspicious(db):
    a = _mk_svc(db, name="a", stale_class=STALE_CLASS_ACTIVE)
    _mk_svc(db, name="b", stale_class=STALE_CLASS_EXPECTED)
    c = _mk_svc(db, name="c", stale_class=STALE_CLASS_SUSPICIOUS, team_owner="squad-1")
    d = _mk_svc(db, name="d", stale_class=STALE_CLASS_SUSPICIOUS, team_owner=None)
    _mk_svc(db, name="e", stale_class=None)

    # last deploy для c — 90 дней назад; для d — никогда.
    db.add(Deployment(
        service_id=c.id, started_at=datetime(2026, 2, 1),
        sha=None, status="SUCCESS",
    ))
    # a — свежий deploy (но a active, всё равно не попадёт в top)
    db.add(Deployment(
        service_id=a.id, started_at=datetime(2026, 5, 1),
        sha="abc", status="SUCCESS",
    ))
    db.commit()
    _ = d

    r = build_report(db)
    assert r.stale_active == 1
    assert r.stale_expected == 1
    assert r.stale_suspicious == 2
    assert r.stale_null == 1

    # top-10 — d (никогда) первым (last_deploy_at IS NULL ASC FIRST в нашем sort),
    # потом c (2026-02-01). Не проверяем точный порядок NULL'ов
    # (SQLite vs PG разный), но обоих стейл-сервисов должно быть видно.
    names = {row["name"] for row in r.top_suspicious_stale}
    assert names == {"c", "d"}


# ── alert enrichment quality ─────────────────────────────────────────────────


def test_alert_enrichment_window_and_counts(db):
    now = datetime(2026, 5, 24, 12, 0, 0)
    a = _mk_svc(db, name="svc-a", namespace="squad-1", team_owner="squad-1")
    b = _mk_svc(db, name="svc-b", namespace="squad-1", team_owner=None)
    ingress = _mk_svc(db, name="ingress:foo.com", synthetic=True)

    # blast_radius: ingress --routes_to--> a, plus a --serves_traffic--> ingress
    _mk_edge(db, ingress.id, a.id, "routes_to", extras={"host": "foo.com"})
    _mk_edge(db, a.id, ingress.id, "serves_traffic")

    # nats: a --uses_nats--> subject:x
    subj = _mk_svc(db, name="subject:x", synthetic=True)
    _mk_edge(db, a.id, subj.id, "uses_nats")

    # 3 alerts: 2 в окне 24h, 1 старый
    db.add(AlertEvent(
        service_id=a.id, alertname="al1", fingerprint="fp1",
        fired_at=now - timedelta(hours=2),
    ))
    db.add(AlertEvent(
        service_id=b.id, alertname="al2", fingerprint="fp2",
        fired_at=now - timedelta(hours=10),
    ))
    db.add(AlertEvent(
        service_id=None, alertname="al3", fingerprint="fp3",
        fired_at=now - timedelta(hours=5),
    ))
    db.add(AlertEvent(
        service_id=a.id, alertname="al-old", fingerprint="fp-old",
        fired_at=now - timedelta(days=3),
    ))

    # pod_event для a в окне ±60м от al1
    db.add(PodEvent(
        service_id=a.id, namespace="squad-1", pod_name="a-1",
        reason="OOMKilled", event_uid="uid1",
        first_seen=now - timedelta(hours=2, minutes=15),
    ))
    db.commit()

    r = build_report(db, now=now)
    assert r.alerts_24h_total == 3
    assert r.alerts_24h_with_service == 2  # al1, al2
    assert r.alerts_24h_with_service_pct == round(100 * 2 / 3, 2)
    assert r.alerts_24h_with_owner == 1  # al1 → svc-a (squad-1), al2 → svc-b (None)
    # blast_radius: ingress.id is dst в routes_to → blast_ids = {a.id};
    # alert al1.service_id == a.id (✓); al2.service_id == b.id (нет blast)
    assert r.alerts_24h_with_blast_radius == 1
    # nats: a is src в uses_nats. al1 →a ✓, al2 → b ✗
    assert r.alerts_24h_with_nats_impact == 1
    # pod_trail: pod_event для a в окне ±60м от al1 (fired -2h, event -2h15m)
    assert r.alerts_24h_with_pod_trail == 1


def test_alert_enrichment_empty_alerts(db):
    """0 alerts → все pct = None, count = 0."""
    r = build_report(db, now=datetime(2026, 5, 24))
    assert r.alerts_24h_total == 0
    assert r.alerts_24h_with_service_pct is None
    assert r.alerts_24h_with_owner_pct is None


# ── deploys ──────────────────────────────────────────────────────────────────


def test_deploys_30d_linkage_and_sha(db):
    """Все deploys линкуются к service (FK NOT NULL в схеме); метрика
    ``linked_to_service`` всё равно полезна т.к. ``backfill_tc_deploys.py``
    исторически создавал rows с placeholder service-ом или через UPDATE
    после insert. Здесь покрываем sha-coverage и денежный count window-а.
    """
    a = _mk_svc(db, name="a")
    now = datetime(2026, 5, 24)

    # 3 в окне (все service_id=a.id): 1 с sha, 2 без. 1 старый отсекается.
    db.add(Deployment(service_id=a.id, started_at=now - timedelta(days=5),
                      sha="abc1234", status="SUCCESS"))
    db.add(Deployment(service_id=a.id, started_at=now - timedelta(days=15),
                      sha=None, status="SUCCESS"))
    db.add(Deployment(service_id=a.id, started_at=now - timedelta(days=20),
                      sha=None, status="FAILURE"))
    db.add(Deployment(service_id=a.id, started_at=now - timedelta(days=45),
                      sha="oldsha", status="SUCCESS"))
    db.commit()

    r = build_report(db, now=now)
    assert r.deploys_30d_total == 3
    assert r.deploys_30d_linked_to_service == 3
    assert r.deploys_30d_linked_pct == 100.0
    assert r.deploys_30d_with_sha == 1
    assert r.deploys_30d_with_sha_pct == round(100 * 1 / 3, 2)


# ── rendering ────────────────────────────────────────────────────────────────


def test_render_json_contains_all_keys(db):
    """JSON output schema — все поля QualityReport присутствуют."""
    _mk_svc(db, name="a", team_owner="squad-1")
    db.commit()
    report = build_report(db)
    s = render_json(report)
    parsed = json.loads(s)

    expected_keys = {
        "generated_at",
        "services_total_real",
        "services_total_synthetic",
        "owner_known_count",
        "owner_known_pct",
        "owner_sources",
        "owner_sources_note",
        "edges_by_kind",
        "jobs_total",
        "jobs_linked_to_service",
        "jobs_linked_pct",
        "volume_edges_by_kind",
        "storage_volumes_by_kind",
        "services_without_http_edges",
        "services_without_nats_edges",
        "services_orphan_app",
        "services_app_scope_total",
        "stale_active",
        "stale_expected",
        "stale_suspicious",
        "stale_null",
        "top_suspicious_stale",
        "alerts_24h_total",
        "alerts_24h_with_service",
        "alerts_24h_with_service_pct",
        "alerts_24h_with_owner",
        "alerts_24h_with_owner_pct",
        "alerts_24h_with_blast_radius",
        "alerts_24h_with_blast_radius_pct",
        "alerts_24h_with_nats_impact",
        "alerts_24h_with_nats_impact_pct",
        "alerts_24h_with_pod_trail",
        "alerts_24h_with_pod_trail_pct",
        "deploys_30d_total",
        "deploys_30d_linked_to_service",
        "deploys_30d_linked_pct",
        "deploys_30d_with_sha",
        "deploys_30d_with_sha_pct",
    }
    assert expected_keys.issubset(parsed.keys()), (
        f"missing keys: {expected_keys - parsed.keys()}"
    )


def test_render_markdown_contains_expected_sections(db):
    """Markdown output содержит все 5 секций (по заголовкам)."""
    _mk_svc(db, name="a", team_owner="squad-1")
    db.commit()
    md = render_markdown(build_report(db))
    assert "# KG Quality Report — baseline" in md
    assert "## 1. Service ownership" in md
    assert "## 2. Topology coverage" in md
    assert "## 3. Stale classification" in md
    assert "## 4. Alert enrichment quality" in md
    assert "## 5. Deploy attribution" in md


def test_render_markdown_owner_sources_note_when_all_inferred(db):
    """Когда все owners проставлены без owner_source — markdown показывает
    note про inferred_no_source, и сервис попадает в bucket."""
    _mk_svc(db, name="a", team_owner="squad-1")  # no metadata
    db.commit()
    md = render_markdown(build_report(db))
    assert OWNER_SOURCE_UNTRACKED in md
    assert "без явного" in md


def test_render_markdown_owner_sources_note_when_no_owners(db):
    """Когда нет owners вообще — markdown показывает note про пустоту."""
    _mk_svc(db, name="a")  # no owner
    db.commit()
    md = render_markdown(build_report(db))
    assert "пуст" in md
