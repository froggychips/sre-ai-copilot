"""Тесты на app.services.alert_dedup (L2 + L4).

L2 — Redis suppress-on-recurrence:
  - первый fire → SEND
  - 2-й/3-й в окне → SEND до 3-го, на ≥3-м SUPPRESS_CHRONIC
  - после quiet >2h → SEND_RESURFACED + reset counter
  - Redis down → SEND_NO_DEDUP fail-open
  - пустой service → SEND_NO_DEDUP

L4 — rollout-noise <10min silent:
  - mismatch alert + предыдущий fire длился <10m → SUPPRESS_ROLLOUT
  - mismatch alert + предыдущий длился >10m → проходит к L2
  - non-mismatch alertname → L4 не триггерится

Двухфазность (счётчик считает ФАКТИЧЕСКИ отправленные embed-ы):
  - недоставка → rollback_undelivered снимает tentative-инкремент
  - конкурентный resurface → ровно один SEND_RESURFACED (SET NX на маркер)
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.alert_dedup import (CHRONIC_WINDOW_SECONDS,
                                      RESURFACE_CLAIM_SECONDS, Decision,
                                      decide_send, rollback_undelivered)

_KEY_CNT = "enrich:lastsent:KubePodCrashLooping:bot-service:cnt"
_KEY_LAST = "enrich:lastsent:KubePodCrashLooping:bot-service:last"
_KEY_RESURFACE = "enrich:lastsent:KubePodCrashLooping:bot-service:resurface"


@pytest.fixture
def fake_redis():
    """In-memory mock интерфейса aioredis (get/set/incr/decr/delete).

    set поддерживает nx (SET NX — создать только если ключа нет), incr/decr —
    атомарный счётчик: новый L2-state хранится в двух ключах (`*:cnt`
    counter + `*:last` unix-ts) плюс короткоживущий `*:resurface`-маркер,
    см. app/services/alert_dedup.py.

    Каждый метод начинается с `await asyncio.sleep(0)` — точка передачи
    управления, без неё конкурентные decide_send в gather исполнялись бы
    строго друг за другом и гонку resurface было бы не воспроизвести.
    Сама проверка-и-мутация после yield-а остаётся атомарной (как в Redis).
    """
    store: dict[str, str] = {}
    key_ttls: dict[str, int] = {}

    class FakeRedis:
        # TTL, с которым ключ был создан — тест проверяет, что resurface-маркер
        # живёт минуты, а не всё 6h-окно.
        ttls = key_ttls

        async def get(self, key):
            await asyncio.sleep(0)
            return store.get(key)

        async def set(self, key, value, ex=None, nx=False):
            await asyncio.sleep(0)
            if nx and key in store:
                return None
            store[key] = str(value)
            if ex is not None:
                key_ttls[key] = ex
            return True

        async def incr(self, key):
            await asyncio.sleep(0)
            new = int(store.get(key, "0")) + 1
            store[key] = str(new)
            return new

        async def decr(self, key):
            await asyncio.sleep(0)
            new = int(store.get(key, "0")) - 1
            store[key] = str(new)
            return new

        async def delete(self, key):
            await asyncio.sleep(0)
            store.pop(key, None)
            key_ttls.pop(key, None)

    fake = FakeRedis()
    with patch("app.services.alert_dedup._get_client", return_value=fake):
        yield fake, store


# ── L2: chronic suppress ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_fire_returns_send(fake_redis):
    db = MagicMock()
    d = await decide_send(
        "KubePodCrashLooping", "preprod-kingdom2", "bot-service", "warning", db,
    )
    assert d == Decision.SEND
    # State записан
    _, store = fake_redis
    assert any("bot-service" in k for k in store.keys())


@pytest.mark.asyncio
async def test_chronic_suppress_after_third_fire(fake_redis):
    db = MagicMock()
    # 1st fire
    d1 = await decide_send("KubePodCrashLooping", "ns", "bot-service", "warning", db)
    assert d1 == Decision.SEND
    # 2nd fire через 1 минуту
    fire2 = datetime.now(timezone.utc) + timedelta(minutes=1)
    d2 = await decide_send(
        "KubePodCrashLooping", "ns", "bot-service", "warning", db, fire_at=fire2,
    )
    assert d2 == Decision.SEND
    # 3rd fire через 2 минуты — count=3 → SUPPRESS_CHRONIC
    fire3 = datetime.now(timezone.utc) + timedelta(minutes=2)
    d3 = await decide_send(
        "KubePodCrashLooping", "ns", "bot-service", "warning", db, fire_at=fire3,
    )
    assert d3 == Decision.SUPPRESS_CHRONIC


@pytest.mark.asyncio
async def test_quiet_reset_resurfaced(fake_redis):
    db = MagicMock()
    base = datetime.now(timezone.utc)
    # Записываем фейковый state как-будто было 8 fires, но last_fire >2h назад.
    _, store = fake_redis
    key_cnt = "enrich:lastsent:KubePodCrashLooping:bot-service:cnt"
    key_last = "enrich:lastsent:KubePodCrashLooping:bot-service:last"
    old_last = base - timedelta(hours=3)
    store[key_cnt] = "8"
    store[key_last] = str(int(old_last.timestamp()))
    d = await decide_send(
        "KubePodCrashLooping", "ns", "bot-service", "warning", db, fire_at=base,
    )
    assert d == Decision.SEND_RESURFACED
    # State сброшен: count=1, last = текущий fire
    assert store[key_cnt] == "1"
    assert store[key_last] == str(int(base.timestamp()))


@pytest.mark.asyncio
async def test_redis_down_failopen():
    db = MagicMock()

    class FailingRedis:
        async def get(self, key):
            raise ConnectionError("redis down")

    with patch("app.services.alert_dedup._get_client", return_value=FailingRedis()):
        d = await decide_send(
            "KubePodCrashLooping", "ns", "bot-service", "warning", db,
        )
    assert d == Decision.SEND_NO_DEDUP


@pytest.mark.asyncio
async def test_empty_service_returns_send_no_dedup():
    db = MagicMock()
    d = await decide_send("KubePodCrashLooping", "ns", None, "warning", db)
    assert d == Decision.SEND_NO_DEDUP


# ── L4: rollout-noise silent ────────────────────────────────────────


@pytest.mark.asyncio
async def test_rollout_silent_when_previous_short(fake_redis):
    """Mismatch + предыдущий fire резолвнулся за <10m → SUPPRESS_ROLLOUT."""
    db = MagicMock()
    now = datetime.now(timezone.utc)
    with patch("app.services.alert_dedup.incidents_on") as mock_incidents:
        mock_incidents.return_value = [{
            "alertname": "KubeDeploymentGenerationMismatch",
            "severity": "warning",
            "fingerprint": "x",
            "fired_at": now - timedelta(hours=1),
            "resolved_at": now - timedelta(hours=1) + timedelta(minutes=3),
        }]
        d = await decide_send(
            "KubeDeploymentGenerationMismatch",
            "prod-kingdom5",
            "vm-kube-state-metrics",
            "warning",
            db,
            fire_at=now,
        )
    assert d == Decision.SUPPRESS_ROLLOUT


@pytest.mark.asyncio
async def test_rollout_no_silent_when_previous_long(fake_redis):
    """Mismatch + предыдущий длился >10m → не считаем noise, идём в L2."""
    db = MagicMock()
    now = datetime.now(timezone.utc)
    with patch("app.services.alert_dedup.incidents_on") as mock_incidents:
        mock_incidents.return_value = [{
            "alertname": "KubeDeploymentGenerationMismatch",
            "fingerprint": "x",
            "fired_at": now - timedelta(hours=1),
            "resolved_at": now - timedelta(hours=1) + timedelta(minutes=20),
        }]
        d = await decide_send(
            "KubeDeploymentGenerationMismatch",
            "prod-kingdom5",
            "vm-kube-state-metrics",
            "warning",
            db,
            fire_at=now,
        )
    assert d == Decision.SEND  # L2 first fire


@pytest.mark.asyncio
async def test_rollout_silent_not_triggered_for_crashloop():
    """Non-mismatch alertname → L4 не активируется, идёт L2."""
    db = MagicMock()
    fake = MagicMock()
    fake.get = AsyncMock(return_value=None)
    fake.set = AsyncMock()
    fake.incr = AsyncMock(return_value=1)
    with patch("app.services.alert_dedup._get_client", return_value=fake):
        d = await decide_send(
            "KubePodCrashLooping",
            "preprod-kingdom2",
            "bot-service",
            "warning",
            db,
        )
    assert d == Decision.SEND


@pytest.mark.asyncio
async def test_rollout_check_runs_in_worker_thread(fake_redis):
    """L4-lookup (sync SQL) уходит в thread pool, а не в поток event loop."""
    import threading

    db = MagicMock()
    now = datetime.now(timezone.utc)
    seen: list[int] = []

    def _incidents_on(*a, **kw):
        seen.append(threading.get_ident())
        return []

    with patch("app.services.alert_dedup.incidents_on", new=_incidents_on):
        await decide_send(
            "KubeDeploymentGenerationMismatch",
            "prod-kingdom5",
            "town-service",
            "warning",
            db,
            fire_at=now,
        )
    assert len(seen) == 1
    # Прямой вызов исполнялся бы в потоке loop-а и блокировал его до ~17s.
    assert seen[0] != threading.get_ident()


# ── Фаза подтверждения: счётчик считает ОТПРАВЛЕННЫЕ embed-ы ─────────
#
# Регресс: decide_send инкрементил счётчик ДО отправки, а enrich+send в
# webhooks.py обёрнут в `except Exception → log.warning`. Три подряд
# неудачные доставки → четвёртый (уже живой) fire получал SUPPRESS_CHRONIC
# при НУЛЕ embed-ов в канале, и 6h-окно молчало целиком.


@pytest.mark.asyncio
async def test_undelivered_fires_do_not_grow_chronic_counter(fake_redis):
    """3 недоставки подряд → 4-й fire всё ещё SEND, счётчик не накопился."""
    db = MagicMock()
    _, store = fake_redis
    base = datetime.now(timezone.utc)

    for i in range(3):
        d = await decide_send(
            "KubePodCrashLooping", "ns", "bot-service", "warning", db,
            fire_at=base + timedelta(minutes=i),
        )
        assert d == Decision.SEND
        await rollback_undelivered("KubePodCrashLooping", "bot-service", d)
        # Ноль информации не несёт — ключ удалён (и не остался без TTL).
        assert _KEY_CNT not in store

    d4 = await decide_send(
        "KubePodCrashLooping", "ns", "bot-service", "warning", db,
        fire_at=base + timedelta(minutes=3),
    )
    assert d4 == Decision.SEND, "подавление по несуществующим embed-ам"


@pytest.mark.asyncio
async def test_rollback_removes_exactly_one_increment(fake_redis):
    """Откат снимает ровно свой fire: доставленные embed-ы продолжают копиться."""
    db = MagicMock()
    _, store = fake_redis
    base = datetime.now(timezone.utc)

    def _fire(minute):
        return decide_send(
            "KubePodCrashLooping", "ns", "bot-service", "warning", db,
            fire_at=base + timedelta(minutes=minute),
        )

    assert await _fire(0) == Decision.SEND           # доставлен → count=1
    d2 = await _fire(1)                              # count=2, но упал
    assert d2 == Decision.SEND
    await rollback_undelivered("KubePodCrashLooping", "bot-service", d2)
    assert store[_KEY_CNT] == "1"
    assert await _fire(2) == Decision.SEND           # count=2
    assert await _fire(3) == Decision.SUPPRESS_CHRONIC  # count=3 → хроника


@pytest.mark.asyncio
async def test_rollback_noop_for_suppress_decision(fake_redis):
    """SUPPRESS_* ничего не отправлял и ничего не инкрементил лишнего —
    откат по нему не должен занижать счётчик."""
    db = MagicMock()
    _, store = fake_redis
    base = datetime.now(timezone.utc)
    d = None
    for i in range(3):
        d = await decide_send(
            "KubePodCrashLooping", "ns", "bot-service", "warning", db,
            fire_at=base + timedelta(minutes=i),
        )
    assert d == Decision.SUPPRESS_CHRONIC
    await rollback_undelivered("KubePodCrashLooping", "bot-service", d)
    assert store[_KEY_CNT] == "3"


@pytest.mark.asyncio
async def test_rollback_without_service_is_noop(fake_redis):
    """SEND_NO_DEDUP (пустой service) — ключа нет, откат не должен падать."""
    await rollback_undelivered("KubePodCrashLooping", None, Decision.SEND)
    _, store = fake_redis
    assert store == {}


@pytest.mark.asyncio
async def test_rollback_redis_down_is_failopen():
    """Сбой Redis в откате не выпускает исключение в вызывающий хендлер."""
    class FailingRedis:
        async def decr(self, key):
            raise ConnectionError("redis down")

    with patch("app.services.alert_dedup._get_client", return_value=FailingRedis()):
        await rollback_undelivered(
            "KubePodCrashLooping", "bot-service", Decision.SEND,
        )


# ── Resurface: атомарный single-winner ──────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_resurface_yields_single_resurfaced(fake_redis):
    """Два конкурентных fire после >2h тишины → ровно один 🌀-embed.

    Раньше ветка resurface была GET→check→SET: обе реплики, разобравшие
    один AM-batch, видели старый `last` и обе возвращали SEND_RESURFACED.
    """
    db = MagicMock()
    _, store = fake_redis
    base = datetime.now(timezone.utc)
    store[_KEY_CNT] = "8"
    store[_KEY_LAST] = str(int((base - timedelta(hours=3)).timestamp()))

    d1, d2 = await asyncio.gather(
        decide_send(
            "KubePodCrashLooping", "ns", "bot-service", "warning", db,
            fire_at=base,
        ),
        decide_send(
            "KubePodCrashLooping", "ns", "bot-service", "warning", db,
            fire_at=base + timedelta(seconds=1),
        ),
    )

    assert [d1, d2].count(Decision.SEND_RESURFACED) == 1, (d1, d2)
    # Проигравший идёт обычным in-window путём — второго 🌀 в канале нет.
    assert [d1, d2].count(Decision.SEND) == 1, (d1, d2)
    assert _KEY_RESURFACE in store


@pytest.mark.asyncio
async def test_resurface_marker_ttl_is_short_and_lets_next_resurface(fake_redis):
    """Маркер живёт минуты, а не 6h-окно: следующий legit-resurface не глушится."""
    db = MagicMock()
    fake, store = fake_redis
    base = datetime.now(timezone.utc)
    store[_KEY_CNT] = "8"
    store[_KEY_LAST] = str(int((base - timedelta(hours=3)).timestamp()))

    assert await decide_send(
        "KubePodCrashLooping", "ns", "bot-service", "warning", db, fire_at=base,
    ) == Decision.SEND_RESURFACED
    assert fake.ttls[_KEY_RESURFACE] == RESURFACE_CLAIM_SECONDS
    assert fake.ttls[_KEY_RESURFACE] < CHRONIC_WINDOW_SECONDS

    # Маркер истёк (fake TTL не тикает — эмулируем), снова >2h тишины.
    store.pop(_KEY_RESURFACE)
    store[_KEY_LAST] = str(int((base + timedelta(hours=5)).timestamp()))
    assert await decide_send(
        "KubePodCrashLooping", "ns", "bot-service", "warning", db,
        fire_at=base + timedelta(hours=8),
    ) == Decision.SEND_RESURFACED


@pytest.mark.asyncio
async def test_rollback_of_resurfaced_frees_marker(fake_redis):
    """Недоставленный 🌀-embed → маркер снят, следующий fire снова получит 🌀."""
    db = MagicMock()
    _, store = fake_redis
    base = datetime.now(timezone.utc)
    store[_KEY_CNT] = "8"
    store[_KEY_LAST] = str(int((base - timedelta(hours=3)).timestamp()))

    d = await decide_send(
        "KubePodCrashLooping", "ns", "bot-service", "warning", db, fire_at=base,
    )
    assert d == Decision.SEND_RESURFACED
    await rollback_undelivered("KubePodCrashLooping", "bot-service", d)
    assert _KEY_RESURFACE not in store
    assert _KEY_CNT not in store

    # Тишина всё ещё >2h (последний fire не дошёл до канала) — 🌀 повторяем.
    store[_KEY_LAST] = str(int((base - timedelta(hours=3)).timestamp()))
    assert await decide_send(
        "KubePodCrashLooping", "ns", "bot-service", "warning", db,
        fire_at=base + timedelta(minutes=1),
    ) == Decision.SEND_RESURFACED


# ── enrich-and-forward: подтверждение доставки на уровне хендлера ─────
#
# Хендлер зовёт decide_send ДО enrich+send, а весь блок enrich+send
# обёрнут в `except Exception → log.warning`. Проверяем, что упавшая
# доставка откатывает tentative-инкремент, т.е. глухоты на 6h-окно
# после серии сбоев Discord-а больше нет.


def _enrich_payload(fingerprint: str):
    from app.models.incident import AlertManagerWebhook

    return AlertManagerWebhook(
        version="4",
        groupKey=f"grp-{fingerprint}",
        status="firing",
        alerts=[
            {
                "status": "firing",
                "labels": {
                    "alertname": "KubePodCrashLooping",
                    "severity": "critical",
                    "namespace": "prod-kingdom1",
                    "service": "town-service",
                    "pod": "town-service-6c6cd4df-8hx9c",
                },
                "annotations": {"summary": "s", "description": "d"},
                "startsAt": "2026-08-07T10:00:00Z",
                "endsAt": None,
                "generatorURL": "https://prom.local",
                "fingerprint": fingerprint,
            }
        ],
    )


@pytest.fixture
def enrich_handler(monkeypatch):
    """Хендлер enrich-and-forward со замоканным окружением (без PG/LLM/Discord)."""
    import app.api.webhooks as webhooks
    import app.knowledge_graph.auto_populator as populator_mod
    import app.services.alert_enrichment as enrichment_mod

    monkeypatch.setattr(webhooks.settings, "DISCORD_ENRICH_ENABLED", True)
    monkeypatch.setattr(populator_mod, "populate_from_incident", lambda db, inc: {})
    monkeypatch.setattr(enrichment_mod, "enrich_alert", lambda db, inc: MagicMock())
    return webhooks


async def _fire(webhooks, fingerprint: str):
    return await webhooks.alertmanager_webhook_enrich_and_forward(
        _enrich_payload(fingerprint), db=MagicMock(),
    )


def _patch_send(monkeypatch, impl):
    import app.services.discord_service as discord_mod

    monkeypatch.setattr(discord_mod.DiscordService, "send_enriched_alert", impl)


@pytest.mark.asyncio
async def test_handler_send_exception_does_not_arm_chronic_suppress(
    enrich_handler, fake_redis, monkeypatch,
):
    """3 упавшие доставки → 4-й fire всё ещё шлёт embed (не SUPPRESS_CHRONIC)."""
    _, store = fake_redis
    attempts: list[str] = []

    async def _boom(self, ctxs, env=None, resurfaced=False):
        attempts.append("fail")
        raise RuntimeError("discord 503")

    _patch_send(monkeypatch, _boom)
    for i in range(3):
        res = await _fire(enrich_handler, f"FP-FAIL-{i}")
        assert res["enriched_groups"] == 0
        assert res["suppressed_chronic"] == 0
    assert len(attempts) == 3
    assert "enrich:lastsent:KubePodCrashLooping:town-service:cnt" not in store

    # Discord ожил — четвёртый fire обязан дойти до канала.
    async def _ok(self, ctxs, env=None, resurfaced=False):
        attempts.append("ok")

    _patch_send(monkeypatch, _ok)
    res = await _fire(enrich_handler, "FP-FAIL-OK")
    assert res["suppressed_chronic"] == 0
    assert res["enriched_groups"] == 1
    assert attempts[-1] == "ok"


@pytest.mark.asyncio
async def test_handler_explicit_false_counts_as_not_delivered(
    enrich_handler, fake_redis, monkeypatch,
):
    """Явный `delivered=False` (контракт send_*_report) = недоставка → откат."""
    _, store = fake_redis

    async def _not_delivered(self, ctxs, env=None, resurfaced=False):
        return False

    _patch_send(monkeypatch, _not_delivered)
    res = await _fire(enrich_handler, "FP-FALSE-1")
    assert res["enriched_groups"] == 0, "недоставленный embed не считается отправленным"
    assert "enrich:lastsent:KubePodCrashLooping:town-service:cnt" not in store


@pytest.mark.asyncio
async def test_handler_delivered_send_still_counts(
    enrich_handler, fake_redis, monkeypatch,
):
    """Регресс-гвард на противоположную сторону: доставленные embed-ы копятся
    и на третьем группа уходит в SUPPRESS_CHRONIC (штатное поведение L2)."""
    _, store = fake_redis
    sent: list[bool] = []

    async def _ok(self, ctxs, env=None, resurfaced=False):
        sent.append(True)
        return None  # текущий контракт send_enriched_alert

    _patch_send(monkeypatch, _ok)
    for i in range(2):
        res = await _fire(enrich_handler, f"FP-OK-{i}")
        assert res["enriched_groups"] == 1
    res = await _fire(enrich_handler, "FP-OK-3")
    assert res["suppressed_chronic"] == 1
    assert res["enriched_groups"] == 0
    assert len(sent) == 2
    assert store["enrich:lastsent:KubePodCrashLooping:town-service:cnt"] == "3"
