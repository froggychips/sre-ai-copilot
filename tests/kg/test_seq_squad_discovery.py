"""Seq стендов: список из графа, отдельный счёт, только Error/Fatal.

До 24.09.2026 kg_log_observations по squad-* был пуст: синк знал только
девять прод-инстансов из конфига, а у каждого стенда свой Seq в namespace-е.
"""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph import seq_logs_sync
from app.knowledge_graph.schema import NODE_KIND_SERVICE, Namespace, Service


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _seq(db, ns, *, state="active", kind=NODE_KIND_SERVICE, name="seq"):
    if not db.get(Namespace, ns):
        db.add(Namespace(namespace=ns, state=state))
    db.add(Service(namespace=ns, name=name, node_kind=kind))
    db.flush()


def _enabled(**kw):
    vals = {"SEQ_SQUAD_DISCOVERY_ENABLED": True, "SEQ_SQUAD_MAX_INSTANCES": 150}
    vals.update(kw)
    return patch.multiple(seq_logs_sync.settings, **vals)


def test_discovery_off_by_default(db):
    _seq(db, "squad-1-shared")
    with patch.object(seq_logs_sync.settings, "SEQ_SQUAD_DISCOVERY_ENABLED", False):
        assert seq_logs_sync._discover_squad_instances(db) == []


def test_discovery_active_squad_seq_only(db):
    _seq(db, "squad-1-shared")
    _seq(db, "squad-1-kingdom2")
    _seq(db, "squad-9-shared", state="missing")         # снесённый стенд
    _seq(db, "prod-shared")                              # не сквад
    _seq(db, "squad-2-shared", name="seq-ui")            # не сам seq
    _seq(db, "squad-3-shared", kind="workload")          # workload-узел
    with _enabled():
        got = seq_logs_sync._discover_squad_instances(db)
    assert [(i["name"], i["url"], i["namespace"], i["token"]) for i in got] == [
        ("squad:squad-1-kingdom2", "http://seq.squad-1-kingdom2.svc.cluster.local",
         "squad-1-kingdom2", None),
        ("squad:squad-1-shared", "http://seq.squad-1-shared.svc.cluster.local",
         "squad-1-shared", None),
    ]
    assert all(i["kind"] == "squad" for i in got)


def test_discovery_respects_cap(db):
    for n in range(5):
        _seq(db, f"squad-{n}-shared")
    with _enabled(SEQ_SQUAD_MAX_INSTANCES=2):
        assert len(seq_logs_sync._discover_squad_instances(db)) == 2


def test_squad_instance_queries_only_error_fatal():
    calls = []

    class _Win:
        measured = True
        value = {}
        reason = None

    class _Prov:
        async def service_stats(self, *, level, since, until, limit):
            calls.append(level)
            return _Win()

    import asyncio
    from datetime import datetime
    with patch.object(seq_logs_sync, "make_log_provider", return_value=_Prov()) as mk:
        asyncio.run(seq_logs_sync._sync_instance(
            None, {"name": "squad:x", "url": "http://seq.x", "kind": "squad"},
            datetime(2026, 9, 24), datetime(2026, 9, 24), datetime(2026, 9, 24),
        ))
    assert calls == ["Error", "Fatal"]
    assert mk.call_args.kwargs["timeout"] == seq_logs_sync._SQUAD_TIMEOUT_S


_OK = {"groups_total": 0, "matched": 0, "unmatched": 0, "rows": 2}
_PROD = [{"name": "shared", "url": "https://seq.example", "token": "t"}]
_SQUADS = [
    {"name": f"squad:s{i}", "url": f"http://seq.s{i}", "namespace": f"s{i}", "kind": "squad"}
    for i in range(3)
]


def _run(db, prod, squads, side_effect):
    with patch.object(seq_logs_sync, "_load_instances", return_value=prod), \
         patch.object(seq_logs_sync, "_discover_squad_instances", return_value=squads), \
         patch.object(seq_logs_sync, "_sync_instance", AsyncMock(side_effect=side_effect)):
        return seq_logs_sync.sync_seq_logs(db, window_minutes=10)


def test_squad_failures_do_not_degrade_prod_run(db):
    """Все стенды недоступны (типично — NetworkPolicy), прод ответил: прогон
    прода чистый, слепота стендов — в своём счёте, а не error-маркер."""
    stats = _run(db, _PROD, _SQUADS, [dict(_OK)] + [RuntimeError("timeout")] * 3)
    assert stats["reached"] == 1 and stats["failed"] == 0
    assert "error" not in stats
    assert stats["squads"] == {"instances": 3, "reached": 0, "failed": 3, "rows": 0}


def test_squad_rows_counted(db):
    stats = _run(db, _PROD, _SQUADS, [dict(_OK)] * 4)
    assert stats["squads"]["reached"] == 3
    assert stats["rows"] == 8


def test_only_squads_all_unreachable_is_blind(db):
    """Прода в конфиге нет, стенды не ответили — heartbeat писать нельзя."""
    with patch.object(seq_logs_sync.settings, "SEQ_SQUAD_DISCOVERY_ENABLED", True):
        stats = _run(db, [], _SQUADS, [RuntimeError("timeout")] * 3)
    assert "error" in stats


def test_squad_only_success_marks_source_success():
    """Прод не настроен, стенды ответили — источник успешен (heartbeat пишется)."""
    from app.knowledge_graph.source_status import SOURCE_STATUS_KEY, SourceStatus
    from app.workers.tasks import _src_seq
    ok = _src_seq({"instances": 0, "reached": 0, "squads": {"instances": 3, "reached": 3}})
    assert ok[SOURCE_STATUS_KEY] == SourceStatus.SUCCESS.value
    part = _src_seq({"instances": 0, "reached": 0, "squads": {"instances": 3, "reached": 1}})
    assert part[SOURCE_STATUS_KEY] == SourceStatus.PARTIAL.value
    none = _src_seq({"instances": 0, "reached": 0})
    assert none[SOURCE_STATUS_KEY] == SourceStatus.UNAVAILABLE.value
