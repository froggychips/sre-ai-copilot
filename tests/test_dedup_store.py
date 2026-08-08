"""dedup_store: cross-replica PATCH-dedup state (PG + in-memory fallback).

PG-путь на CI недоступен (self-hosted runner без postgres) — здесь
покрывается контракт fallback-пути: семантика get_fresh/save/bump/
update_embed идентична PG-пути, чтобы PG-аварии не меняли поведение.
"""

import pytest

from app.services.discord import dedup as _dedup_state
from app.services.discord import dedup_store


@pytest.fixture(autouse=True)
def _force_memory_fallback(monkeypatch):
    """Любой PG-вызов падает → store работает на in-memory dict."""

    def _boom():
        raise RuntimeError("pg down (test)")

    monkeypatch.setattr(dedup_store, "_pg_session", _boom)
    monkeypatch.setattr(dedup_store, "_pg_warned", True)  # без warning-шума
    _dedup_state._recent_enriched.clear()
    yield
    _dedup_state._recent_enriched.clear()


def test_get_fresh_empty_returns_none():
    assert dedup_store.get_fresh("k1", ttl_sec=1800, now=1000.0) is None


def test_save_then_get_fresh_roundtrip():
    dedup_store.save(
        "k1", msg_id="m1", embed={"title": "t"},
        alertname="A", namespace="ns", severity="critical", now=1000.0,
    )
    rec = dedup_store.get_fresh("k1", ttl_sec=1800, now=1100.0)
    assert rec is not None
    assert rec["msg_id"] == "m1"
    assert rec["count"] == 1
    assert rec["first_ts"] == 1000.0


def test_get_fresh_expired_invisible():
    dedup_store.save("k1", msg_id="m1", embed=None, now=1000.0)
    assert dedup_store.get_fresh("k1", ttl_sec=60, now=1100.0) is None


def test_bump_increments_atomically():
    dedup_store.save("k1", msg_id="m1", embed=None, now=1000.0)
    rec = dedup_store.bump("k1", now=1100.0)
    assert rec["count"] == 2
    assert rec["last_ts"] == 1100.0
    rec = dedup_store.bump("k1", now=1200.0)
    assert rec["count"] == 3


def test_bump_missing_key_returns_none():
    assert dedup_store.bump("nope", now=1000.0) is None


def test_save_overwrites_stale_with_fresh_window():
    dedup_store.save("k1", msg_id="m1", embed=None, now=1000.0)
    dedup_store.bump("k1", now=1100.0)
    # Новое окно: count сбрасывается, msg_id новый.
    dedup_store.save("k1", msg_id="m2", embed=None, now=9000.0)
    rec = dedup_store.get_fresh("k1", ttl_sec=1800, now=9100.0)
    assert rec["msg_id"] == "m2"
    assert rec["count"] == 1


def test_update_embed_persists():
    dedup_store.save("k1", msg_id="m1", embed={"v": 1}, now=1000.0)
    dedup_store.update_embed("k1", {"v": 2})
    rec = dedup_store.get_fresh("k1", ttl_sec=1800, now=1100.0)
    assert rec["embed"] == {"v": 2}


def test_get_fresh_does_not_purge(monkeypatch):
    """Infra H4: get_fresh — чистый SELECT, НЕ удаляет stale-строки.

    Stale-запись остаётся в кэше (невидима для caller-а, но не вычищена) —
    purge теперь обязанность purge_stale/beat, не hot-path.
    """
    # PG-путь: убедимся что get_fresh не дёргает delete() на query.
    delete_calls = []

    class _FakeQuery:
        def filter(self, *a, **k):
            return self

        def delete(self, *a, **k):
            delete_calls.append(1)
            return 0

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def query(self, *a, **k):
            return _FakeQuery()

        def get(self, *a, **k):
            return None

    monkeypatch.setattr(dedup_store, "_pg_session", lambda: _FakeSession())
    monkeypatch.setattr(dedup_store, "_pg_warned", True)
    dedup_store.get_fresh("k1", ttl_sec=1800, now=1000.0)
    assert delete_calls == [], "get_fresh не должен вызывать DELETE (hot-path)"


def test_get_fresh_keeps_stale_row_in_memory():
    """get_fresh видит stale как None, но строку из кэша НЕ удаляет."""
    dedup_store.save("k1", msg_id="m1", embed=None, now=1000.0)
    # stale относительно ttl=60: now-first_ts=100 > 60
    assert dedup_store.get_fresh("k1", ttl_sec=60, now=1100.0) is None
    # строка ещё в кэше — get_fresh её не вычистил
    assert "k1" in _dedup_state._recent_enriched


def test_purge_stale_removes_expired():
    dedup_store.save("old", msg_id="m1", embed=None, now=1000.0)
    dedup_store.save("new", msg_id="m2", embed=None, now=2000.0)
    # ttl=600, now=2100: old (first_ts=1000, age 1100>600) stale; new (age 100) свежий
    removed = dedup_store.purge_stale(ttl_sec=600, now=2100.0)
    assert removed == 1
    assert "old" not in _dedup_state._recent_enriched
    assert "new" in _dedup_state._recent_enriched


def test_purge_stale_noop_when_all_fresh():
    dedup_store.save("k1", msg_id="m1", embed=None, now=1000.0)
    removed = dedup_store.purge_stale(ttl_sec=1800, now=1100.0)
    assert removed == 0
    assert "k1" in _dedup_state._recent_enriched


# ---------------------------------------------------------------------------
# claim/release: атомарный claim-before-post (#9, TOCTOU двух реплик)
# ---------------------------------------------------------------------------

def test_claim_free_key_acquires_and_writes_placeholder():
    """Свободный ключ: claim возвращает None (наш) и ставит placeholder."""
    assert dedup_store.claim("k1", ttl_sec=1800, now=1000.0) is None
    rec = _dedup_state._recent_enriched["k1"]
    assert rec["msg_id"] == ""  # placeholder до финализации save()


def test_claim_mid_post_placeholder_returns_empty_msg_id():
    """Вторая реплика во время POST первой: получает placeholder → skip."""
    assert dedup_store.claim("k1", ttl_sec=1800, now=1000.0) is None
    second = dedup_store.claim("k1", ttl_sec=1800, now=1001.0)
    assert second is not None
    assert second["msg_id"] == ""


def test_claim_after_save_returns_record_for_patch():
    """Финализированная запись: claim отдаёт её с msg_id → PATCH-путь."""
    assert dedup_store.claim("k1", ttl_sec=1800, now=1000.0) is None
    dedup_store.save("k1", msg_id="m1", embed=None, now=1000.0)
    rec = dedup_store.claim("k1", ttl_sec=1800, now=1100.0)
    assert rec is not None
    assert rec["msg_id"] == "m1"


def test_claim_stale_record_reacquires():
    """Протухшая запись — новое окно: claim снова наш."""
    dedup_store.save("k1", msg_id="m1", embed=None, now=1000.0)
    assert dedup_store.claim("k1", ttl_sec=60, now=2000.0) is None
    assert _dedup_state._recent_enriched["k1"]["msg_id"] == ""


def test_release_removes_placeholder_only():
    """release снимает незавершённый claim, но не финализированную запись."""
    assert dedup_store.claim("k1", ttl_sec=1800, now=1000.0) is None
    dedup_store.release("k1")
    assert "k1" not in _dedup_state._recent_enriched

    dedup_store.save("k2", msg_id="m2", embed=None, now=1000.0)
    dedup_store.release("k2")
    assert _dedup_state._recent_enriched["k2"]["msg_id"] == "m2"


def test_claim_pg_path_insert_conflict_is_atomic(monkeypatch):
    """PG-путь: INSERT-конфликт по PK — проигравший видит запись победителя.

    Реальный sqlite-движок (та же семантика UNIQUE PK, что и в PG):
    первый claim INSERT-ит placeholder, конкурент ловит IntegrityError и
    получает строку вместо второго POST-а.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(dedup_store, "_pg_session", TestSession)

    # Победитель клеймит.
    assert dedup_store.claim("kX", ttl_sec=1800, now=1000.0) is None
    # Конкурент: свежий placeholder → dict с пустым msg_id (skip).
    second = dedup_store.claim("kX", ttl_sec=1800, now=1001.0)
    assert second is not None
    assert second["msg_id"] == ""
    # Победитель финализирует; третий вызов получает msg_id для PATCH.
    dedup_store.save("kX", msg_id="m-pg", embed=None, now=1002.0)
    third = dedup_store.claim("kX", ttl_sec=1800, now=1003.0)
    assert third is not None
    assert third["msg_id"] == "m-pg"
    # In-memory fallback не задействован — всё жило в «PG».
    assert "kX" not in _dedup_state._recent_enriched
    engine.dispose()


# ---------------------------------------------------------------------------
# Секрет вебхука не хранится (2026-08-08, миграция 20260808_0200)
#
# Хвост Discord webhook URL — токен на постинг в канал. Пока он лежал в
# `discord_dedup.webhook_url`, любой read-only доступ к БД (реплика, дамп,
# бэкап, SELECT-грант) = право спамить в #infra-error от имени бота.
# ---------------------------------------------------------------------------

# Токен нарочно уникальный: его ищем подстрокой по всему дампу таблицы.
_WEBHOOK = "https://discord.com/api/webhooks/424242/S3CR3T-must-not-be-stored"
_TOKEN = "S3CR3T-must-not-be-stored"


def _sqlite_store(monkeypatch):
    """Подменить PG на реальный sqlite — чтобы читать СЫРЫЕ строки таблицы.

    Перебивает autouse-фикстуру `_force_memory_fallback` (monkeypatch
    применяется позже), так что запись идёт «в БД», а не в память.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(dedup_store, "_pg_session", TestSession)
    return engine


def test_model_has_no_webhook_url_column():
    """ORM-модель не объявляет колонку — регресс-гвард против возврата."""
    columns = {c.name for c in dedup_store.DiscordDedupEntry.__table__.columns}
    assert "webhook_url" not in columns, (
        "webhook_url снесён миграцией 20260808_0200: хранить токен в БД нельзя"
    )


def test_save_signature_rejects_webhook_url():
    """save() физически не принимает секрет — нельзя «случайно» вернуть."""
    with pytest.raises(TypeError):
        dedup_store.save(
            "k1", msg_id="m1", webhook_url=_WEBHOOK, embed=None, now=1000.0,
        )


def test_webhook_token_never_reaches_db(monkeypatch):
    """Полный цикл claim → save → bump → update_embed: в таблице нет токена.

    Сканируем ВСЕ колонки всех строк, а не только бывшую webhook_url —
    иначе регресс мог бы просочиться через embed или debug-поля.
    """
    engine = _sqlite_store(monkeypatch)
    try:
        assert dedup_store.claim(
            "kSec", ttl_sec=1800, now=1000.0,
            alertname="KubePodCrashLooping", namespace="preprod-kingdom1",
            service="auth-service", severity="critical",
        ) is None
        dedup_store.save(
            "kSec",
            msg_id="m-1",
            embed={"title": "KubePodCrashLooping", "footer": {"text": "incident/x"}},
            alertname="KubePodCrashLooping", namespace="preprod-kingdom1",
            service="auth-service", severity="critical", now=1000.0,
        )
        dedup_store.bump("kSec", now=1100.0)
        dedup_store.update_embed("kSec", {"footer": {"text": "incident/x · ×2"}})

        with engine.connect() as conn:
            rows = conn.exec_driver_sql("SELECT * FROM discord_dedup").fetchall()
        assert rows, "строка дедупа должна существовать — иначе тест ничего не проверяет"
        dump = " ".join(str(value) for row in rows for value in row)
        assert _TOKEN not in dump
        assert "/webhooks/" not in dump
    finally:
        engine.dispose()


def test_memory_fallback_record_has_no_webhook_url():
    """In-memory fallback (PG лежит) тоже не держит токен в heap-е."""
    assert dedup_store.claim("k1", ttl_sec=1800, now=1000.0) is None
    assert "webhook_url" not in _dedup_state._recent_enriched["k1"]
    dedup_store.save("k1", msg_id="m1", embed={"v": 1}, now=1000.0)
    assert "webhook_url" not in _dedup_state._recent_enriched["k1"]


def test_dedup_cycle_intact_without_webhook_url(monkeypatch):
    """Дедуп не сломан снятием колонки: claim→save→claim(PATCH)→bump на «PG».

    Тот же сценарий, что гоняет service.py: первый POST клеймит и
    финализирует, повтор попадает в PATCH-ветку (msg_id непустой) и
    инкрементит counter.
    """
    engine = _sqlite_store(monkeypatch)
    try:
        assert dedup_store.claim("kCycle", ttl_sec=1800, now=1000.0) is None
        dedup_store.save("kCycle", msg_id="m-cycle", embed={"v": 1}, now=1000.0)
        again = dedup_store.claim("kCycle", ttl_sec=1800, now=1200.0)
        assert again is not None and again["msg_id"] == "m-cycle"
        bumped = dedup_store.bump("kCycle", now=1200.0)
        assert bumped["count"] == 2
        assert bumped["embed"] == {"v": 1}
    finally:
        engine.dispose()
