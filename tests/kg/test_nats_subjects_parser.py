"""Тесты на app/knowledge_graph/nats_subjects_sync.py.

Покрытие:
  - Регексы на синтетических C#-snippet-ах (unit).
  - Service-name резолвинг по пути файла.
  - Резолвинг `NatsSubjectConst.X` → "y" + skip формат-строк.
  - Интеграционный прогон `parse_monorepo` на закоммиченных C#-fixtures.
  - `persist_to_kg` против in-memory SQLite — корректные synthetic-узлы и
    edges с `extras.direction`.

Никакого сетевого I/O. `_ensure_monorepo` не вызывается.
"""
from __future__ import annotations

from pathlib import Path
from typing import Set

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.knowledge_graph.nats_subjects_sync import (
    NATS_SUBJECTS_NAMESPACE,
    SubjectUsage,
    _load_subject_constants,
    _service_name_from_path,
    parse_csharp_text,
    parse_monorepo,
    persist_to_kg,
)
from app.knowledge_graph.populator import upsert_service
from app.knowledge_graph.schema import Service, ServiceEdge  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures" / "nats_csharp"


# ---------------------------------------------------------------------------
# Service-name резолвинг
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel,expected", [
    ("GR.WO.Map.Service/Consumers/MarchExportConsumer.cs", "map-service"),
    ("GR.WO.MapCoordinator.Service/Program.cs", "mapcoordinator-service"),
    ("GR.WO.City.Workers/Foo.cs", "city-workers"),
    ("GR.WO.Map.Workers/X/Y.cs", "map-workers"),
    ("GR.Platform/DataBus/Nats/NatsService.cs", None),     # skip — не сервис
    ("GR.Platform.Features/Dummy.cs", None),
    ("GR.WO.LoadTests/Some.cs", None),                     # excluded
    ("GR.WO.Map.ClientSandbox/X.cs", None),                # excluded
])
def test_service_name_from_path(rel: str, expected):
    assert _service_name_from_path(Path(rel)) == expected


# ---------------------------------------------------------------------------
# NatsConst.cs resolver
# ---------------------------------------------------------------------------


def test_load_subject_constants_resolves_literals_and_skips_format_strings():
    constants = _load_subject_constants(FIXTURES)
    # Из NatsConst.cs fixtures:
    assert constants["MARCH_EXPORT"] == "march-export"
    assert constants["LEADERBOARD_FINISHED"] == "leaderboardfinished"
    assert constants["ANALYTICS"] == "analytics"
    # Формат-строка с `{` должна быть отфильтрована:
    assert "DYNAMIC_PROFILE" not in constants


# ---------------------------------------------------------------------------
# parse_csharp_text — unit на синтетических snippet-ах
# ---------------------------------------------------------------------------


CONSUMER_SNIPPET = """
public sealed class FooConsumer : MapNatsJetStreamBatchConsumer<FooMessage>
{
    protected override string StreamName    => NatsStreamConst.FOO;
    protected override string Subject       => NatsSubjectConst.FOO;
    protected override string FilterSubject => NatsSubjectConst.FOO;
    protected override string ConsumerName  => nameof(FooConsumer);
}
"""

CONSUMER_LITERAL_SNIPPET = """
public sealed class BarConsumer : NatsJetStreamConsumer<BarMsg>
{
    protected override string Subject => "bar-literal";
}
"""

PUBLISHER_NAMED_SNIPPET = """
public class P {
    private readonly NatsService _natsService;
    public async Task Run() {
        await _natsService.SendToJetStreamAsync(realmId: NatsConst.SharedRealmId,
            subject: NatsSubjectConst.FOO,
            message: new object(),
            messageId: Guid.NewGuid());
    }
}
"""

PUBLISHER_POSITIONAL_SNIPPET = """
public class Q {
    public async Task Run() {
        await app.Services.GetService<NatsService>().SendToJetStreamAsync(NatsConst.SharedRealmId,
            NatsSubjectConst.BAZ,
            new object(),
            messageId: Guid.NewGuid());
    }
}
"""

PUBLISHER_LITERAL_SNIPPET = """
public class R {
    public async Task Run() {
        await _natsService.SendToJetStreamAsync(0, "literal-subject", new {}, Guid.NewGuid());
    }
}
"""


def test_parse_consumer_via_const():
    usages = parse_csharp_text(
        text=CONSUMER_SNIPPET,
        rel_path="GR.WO.Foo.Service/X.cs",
        service_name="foo-service",
        constants={"FOO": "foo"},
        unresolved=set(),
    )
    assert SubjectUsage("foo-service", "foo", "sub", "GR.WO.Foo.Service/X.cs") in usages


def test_parse_consumer_via_literal():
    usages = parse_csharp_text(
        text=CONSUMER_LITERAL_SNIPPET,
        rel_path="GR.WO.Bar.Service/Y.cs",
        service_name="bar-service",
        constants={},
        unresolved=set(),
    )
    assert SubjectUsage("bar-service", "bar-literal", "sub", "GR.WO.Bar.Service/Y.cs") in usages


def test_parse_publisher_named_form():
    usages = parse_csharp_text(
        text=PUBLISHER_NAMED_SNIPPET,
        rel_path="GR.WO.X.Service/A.cs",
        service_name="x-service",
        constants={"FOO": "foo"},
        unresolved=set(),
    )
    assert SubjectUsage("x-service", "foo", "pub", "GR.WO.X.Service/A.cs") in usages


def test_parse_publisher_positional_form():
    usages = parse_csharp_text(
        text=PUBLISHER_POSITIONAL_SNIPPET,
        rel_path="GR.WO.Y.Service/B.cs",
        service_name="y-service",
        constants={"BAZ": "baz"},
        unresolved=set(),
    )
    assert SubjectUsage("y-service", "baz", "pub", "GR.WO.Y.Service/B.cs") in usages


def test_parse_publisher_literal():
    usages = parse_csharp_text(
        text=PUBLISHER_LITERAL_SNIPPET,
        rel_path="GR.WO.Z.Service/C.cs",
        service_name="z-service",
        constants={},
        unresolved=set(),
    )
    assert SubjectUsage("z-service", "literal-subject", "pub", "GR.WO.Z.Service/C.cs") in usages


def test_parse_skips_files_outside_grwo():
    """Файл без service_name (например `GR.Platform/...`) → пустой результат
    даже если внутри есть SendToJetStreamAsync. Это намеренно — общий код
    в `GR.Platform/` не относится к конкретному деплою."""
    usages = parse_csharp_text(
        text=PUBLISHER_NAMED_SNIPPET,
        rel_path="GR.Platform/DataBus/Helper.cs",
        service_name=None,
        constants={"FOO": "foo"},
        unresolved=set(),
    )
    assert usages == []


def test_parse_unresolved_constant_tracked():
    unresolved: Set[str] = set()
    usages = parse_csharp_text(
        text=CONSUMER_SNIPPET,
        rel_path="GR.WO.Foo.Service/X.cs",
        service_name="foo-service",
        constants={},  # FOO не задан
        unresolved=unresolved,
    )
    assert usages == []
    assert "FOO" in unresolved


# ---------------------------------------------------------------------------
# Интеграционный прогон parse_monorepo на закоммиченных fixtures
# ---------------------------------------------------------------------------


def test_parse_monorepo_on_fixtures():
    """Полный прогон парсера на закоммиченных C# fixtures.

    Ожидаем:
      - map-service подписан на march-export и city-fire-stop
      - mapcoordinator-service публикует leaderboardrefreshed + leaderboardfinished,
        подписан на eventfinished
      - analytics-service подписан на analytics + публикует analytics-result
      - GR.Platform.Features/Dummy.cs игнорируется (не GR.WO.*)
    """
    parsed = parse_monorepo(FIXTURES)

    # Все находки конвертим в set tuple-ов для compactного assert-а.
    triples = {(u.service, u.subject, u.direction) for u in parsed.usages}

    # Subscribers
    assert ("map-service", "march-export", "sub") in triples
    assert ("map-service", "city-fire-stop", "sub") in triples
    assert ("mapcoordinator-service", "eventfinished", "sub") in triples
    assert ("analytics-service", "analytics", "sub") in triples

    # Publishers
    assert ("mapcoordinator-service", "leaderboardrefreshed", "pub") in triples
    assert ("mapcoordinator-service", "leaderboardfinished", "pub") in triples
    assert ("analytics-service", "analytics-result", "pub") in triples

    # Negative: ничего из GR.Platform.Features
    for u in parsed.usages:
        assert not u.source_file.startswith("GR.Platform")

    # Sanity-counts
    assert parsed.files_scanned >= 5
    assert parsed.files_with_findings >= 5
    assert len(parsed.services) >= 3
    assert len(parsed.subjects) >= 5


# ---------------------------------------------------------------------------
# persist_to_kg — DB integration (in-memory SQLite)
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    """Свежая SQLite БД из Base.metadata. Совпадает с tests/test_knowledge_graph.py."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_persist_skips_unknown_services(db):
    """Если сервиса нет в kg_services — edge не создаётся, считаем skip."""
    stats = persist_to_kg(db, [
        SubjectUsage("ghost-service", "ghost-subj", "pub", "GR.WO.X/Y.cs"),
    ])
    # Subject узел всё равно мы создаём (idempotent), но edge — нет.
    assert stats["edges_upserted"] == 0
    assert stats["services_skipped_unknown"] == 1
    # Subject-узел существует, синтетический.
    sn = db.query(Service).filter_by(
        namespace=NATS_SUBJECTS_NAMESPACE, name="subject:ghost-subj"
    ).one()
    assert sn.synthetic is True


def test_persist_creates_edge_with_direction_extras(db):
    # Создаём реальный сервис в KG (kg_sync уже его видел).
    svc = upsert_service(db, "squad-1", "map-service", team_owner="map")
    db.commit()

    stats = persist_to_kg(db, [
        SubjectUsage("map-service", "march-export", "sub", "GR.WO.Map.Service/X.cs"),
        SubjectUsage("map-service", "march-export", "sub", "GR.WO.Map.Service/Y.cs"),  # дубль → weight=2
    ])
    assert stats["edges_upserted"] == 1
    assert stats["services_skipped_unknown"] == 0

    edge = (
        db.query(ServiceEdge)
        .filter_by(src_id=svc.id, kind="uses_nats")
        .one()
    )
    assert edge.weight == 2
    assert edge.extras["direction"] == "sub"
    assert edge.extras["subject"] == "march-export"
    assert edge.discovered_by == "kg_sync/nats_subjects_parser"
    # last_seen_at заполнен upsert_edge-ом.
    assert edge.last_seen_at is not None

    # subject-узел — synthetic в `nats-subjects`.
    sn = db.query(Service).filter_by(
        namespace=NATS_SUBJECTS_NAMESPACE, name="subject:march-export"
    ).one()
    assert sn.id == edge.dst_id
    assert sn.synthetic is True


def test_persist_creates_separate_edges_per_namespace(db):
    """Если сервис live в N namespace-ах — каждый получает свой uses_nats edge."""
    s1 = upsert_service(db, "squad-1", "map-service")
    s2 = upsert_service(db, "squad-2", "map-service")
    db.commit()

    stats = persist_to_kg(db, [
        SubjectUsage("map-service", "march-export", "sub", "GR.WO.Map.Service/X.cs"),
    ])
    assert stats["edges_upserted"] == 2
    src_ids = {
        e.src_id for e in db.query(ServiceEdge).filter_by(kind="uses_nats").all()
    }
    assert src_ids == {s1.id, s2.id}


def test_persist_separate_edges_for_pub_and_sub(db):
    """Pub и sub — отдельные edges (different `kind`? нет, разный extras.direction).

    Currently у нас один kind=`uses_nats` с extras.direction. UNIQUE по
    (src, dst, kind) — значит pub+sub перезапишет direction друг друга.
    Это accepted trade-off (см. docstring nats_subjects_sync.py): мы
    предпочитаем плоский граф direction-агностичный, а direction только
    в extras как hint. Если же сервис И публикует И слушает один subject
    (echo-pattern, чат-команды) — последний из них перезаписывает direction.

    Этот тест документирует поведение, чтобы будущие изменения были
    осознанными.
    """
    upsert_service(db, "squad-1", "echo-service")
    db.commit()

    persist_to_kg(db, [
        SubjectUsage("echo-service", "ping", "pub", "X.cs"),
        SubjectUsage("echo-service", "ping", "sub", "Y.cs"),
    ])
    edges = db.query(ServiceEdge).filter_by(kind="uses_nats").all()
    assert len(edges) == 1
    # последний direction = sub (порядок не гарантирован, но в этом случае
    # порядок ввода детерминированный через grouped dict-обход → проверяем
    # что direction ∈ {pub, sub}).
    assert edges[0].extras["direction"] in {"pub", "sub"}
