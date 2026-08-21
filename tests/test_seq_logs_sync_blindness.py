"""Синк логов обязан различать «в логах тихо» и «спросить не удалось».

Прецедент 20.08.2026. NetworkPolicy перекрыла egress к восьми инстансам Seq
(они живут в этом же кластере и доступны только через публичный VIP своего
ingress). Задача при этом завершалась SUCCESS с `rows=0`, heartbeat писался
как за нормальный прогон, и 12,8 часа никто не знал, что логи вне обзора —
отставание доросло до 751 минуты. События в это время были: на shared-инстансе
за окно простоя 23 Error и 49 Warning.
"""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph import seq_logs_sync


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


_THREE = [
    {"name": "kingdom1", "url": "https://seq1.example", "token": "t"},
    {"name": "kingdom2", "url": "https://seq2.example", "token": "t"},
    {"name": "shared", "url": "https://seq3.example", "token": "t"},
]


def _run(db, instance_side_effect):
    with patch.object(seq_logs_sync, "_load_instances", return_value=_THREE), \
         patch.object(seq_logs_sync, "_sync_instance",
                      AsyncMock(side_effect=instance_side_effect)):
        return seq_logs_sync.sync_seq_logs(db, window_minutes=10)


_OK = {"groups_total": 0, "matched": 0, "unmatched": 0, "rows": 0}


def test_all_instances_unreachable_returns_error_marker(db):
    """Ни один не ответил → error-маркер, чтобы heartbeat не записался.

    `_record_beat_heartbeat` пропускает прогоны, вернувшие `error`, — так
    self-health увидит отставание вместо тишины.
    """
    stats = _run(db, [RuntimeError("connect timeout")] * 3)
    assert stats["reached"] == 0
    assert stats["failed"] == 3
    assert "error" in stats, "слепой прогон снова выглядит как успешный"
    assert "недоступны" in stats["error"]


def test_reachable_but_quiet_is_a_clean_success(db):
    """Опросили всех, событий нет — это норма, никакого error."""
    stats = _run(db, [dict(_OK), dict(_OK), dict(_OK)])
    assert stats["reached"] == 3
    assert stats["failed"] == 0
    assert stats["rows"] == 0
    assert "error" not in stats


def test_partial_failure_still_counts_as_a_run(db):
    """Часть инстансов упала — данные собраны не полностью, но обзор есть.

    Error-маркер тут был бы вреден: он остановил бы heartbeat, и один
    недоступный сквад читался бы как полная слепота.
    """
    stats = _run(db, [RuntimeError("timeout"), dict(_OK), dict(_OK)])
    assert stats["reached"] == 2
    assert stats["failed"] == 1
    assert "error" not in stats


def test_rows_are_summed_only_over_reached_instances(db):
    stats = _run(db, [
        RuntimeError("timeout"),
        {"groups_total": 1, "matched": 1, "unmatched": 0, "rows": 2},
        {"groups_total": 1, "matched": 0, "unmatched": 1, "rows": 3},
    ])
    assert stats["rows"] == 5
    assert stats["matched"] == 1
    assert stats["unmatched"] == 1


def test_no_instances_configured_is_a_skip_not_an_error(db):
    """Seq не настроен — это конфигурация, а не отказ."""
    with patch.object(seq_logs_sync, "_load_instances", return_value=[]):
        stats = seq_logs_sync.sync_seq_logs(db, window_minutes=10)
    assert stats == {"skipped": "no_instances"}
