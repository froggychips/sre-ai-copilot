"""Тесты на резолв имени ноды для нодовых алертов.

Инцидент 13.08.2026: `NodeSystemSaturation` приехал в Discord как
«· 192.168.74.165:9100» — по IP пода-экспортёра непонятно, о какой ноде речь
(это была dev-14). Наши VMRule связку уже несут в expr, но группа
`node-exporter` приезжает из чарта victoria-metrics-k8s-stack, поэтому резолв
живёт на стороне копилота.

Покрывает:
  - метка `node` дописывается в labels по метке `pod`;
  - VM опрашивается ОДИН раз на storm (карта кэшируется);
  - мёртвая VM / выключенный флаг = алерт уходит с IP, без исключения;
  - под node-exporter'а больше не даёт фантомный сервис «vm-node».
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.incident import Incident
from app.services import node_resolver
from app.services.alert_enrichment import (EnrichedContext,
                                           _resolve_target_service_from_labels)
from app.services.discord_service import DiscordService
from app.services.node_resolver import (annotate_node_label,
                                        is_node_exporter_pod,
                                        resolve_node_name)

_NODE_LABELS = {
    "alertname": "NodeSystemSaturation",
    "instance": "192.168.74.165:9100",
    "namespace": "monitoring",
    "pod": "vm-node-exporter-gbn9k",
    "service": "vm-node-exporter",
}


@pytest.fixture(autouse=True)
def _clean_cache():
    """Кэш карты нод — модульный, между тестами его надо сбрасывать."""
    node_resolver.reset_cache()
    yield
    node_resolver.reset_cache()


@pytest.fixture
def _vm_enabled(monkeypatch):
    monkeypatch.setattr(node_resolver.settings, "NODE_NAME_RESOLVE_ENABLED", True)
    monkeypatch.setattr(
        node_resolver.settings, "VICTORIA_METRICS_URL", "http://vm:8428",
    )


def _vm_returning(mapping):
    """Патч VMClient.resolve_node_names → mapping."""
    return patch.object(
        node_resolver.VMClient,
        "resolve_node_names",
        new=AsyncMock(return_value=mapping),
    )


class TestPodMatching:
    def test_exporter_pod_recognised(self):
        assert is_node_exporter_pod("vm-node-exporter-gbn9k")

    def test_workload_pod_not_touched(self):
        # Обычный под не должен уводить нас в резолв ноды.
        assert not is_node_exporter_pod("town-grainhost-7f57d67764-sblrn")
        assert not is_node_exporter_pod("town-db-postgresql-0")
        assert not is_node_exporter_pod(None)


class TestResolve:
    @pytest.mark.asyncio
    async def test_node_label_added(self, _vm_enabled):
        labels = dict(_NODE_LABELS)
        with _vm_returning({"vm-node-exporter-gbn9k": "dev-14"}):
            node = await annotate_node_label(labels)
        assert node == "dev-14"
        assert labels["node"] == "dev-14"

    @pytest.mark.asyncio
    async def test_existing_node_label_wins(self, _vm_enabled):
        """Алерт уже с меткой `node` (наши VMRule) — в VM не ходим вообще."""
        labels = {**_NODE_LABELS, "node": "dev-1"}
        with _vm_returning({"vm-node-exporter-gbn9k": "dev-14"}) as mock:
            assert await resolve_node_name(labels) is None
        mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_storm_hits_vm_once(self, _vm_enabled):
        """Кэш: 20 алертов подряд = один поход в VM."""
        with _vm_returning({"vm-node-exporter-gbn9k": "dev-14"}) as mock:
            for _ in range(20):
                labels = dict(_NODE_LABELS)
                assert await annotate_node_label(labels) == "dev-14"
        assert mock.await_count == 1

    @pytest.mark.asyncio
    async def test_vm_down_keeps_alert(self, _vm_enabled):
        """Мёртвая VM: метки нет, исключения нет — алерт уходит с IP."""
        labels = dict(_NODE_LABELS)
        with patch.object(
            node_resolver.VMClient,
            "resolve_node_names",
            new=AsyncMock(side_effect=RuntimeError("connection refused")),
        ):
            assert await annotate_node_label(labels) is None
        assert "node" not in labels

    @pytest.mark.asyncio
    async def test_empty_map_is_cached(self, _vm_enabled):
        """Пустой ответ тоже кэшируется — не долбим мёртвую VM на каждый алерт."""
        with _vm_returning({}) as mock:
            for _ in range(5):
                await annotate_node_label(dict(_NODE_LABELS))
        assert mock.await_count == 1

    @pytest.mark.asyncio
    async def test_kill_switch(self, monkeypatch):
        monkeypatch.setattr(
            node_resolver.settings, "NODE_NAME_RESOLVE_ENABLED", False,
        )
        monkeypatch.setattr(
            node_resolver.settings, "VICTORIA_METRICS_URL", "http://vm:8428",
        )
        with _vm_returning({"vm-node-exporter-gbn9k": "dev-14"}) as mock:
            assert await resolve_node_name(dict(_NODE_LABELS)) is None
        mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_vm_url_configured(self, monkeypatch):
        monkeypatch.setattr(node_resolver.settings, "NODE_NAME_RESOLVE_ENABLED", True)
        monkeypatch.setattr(node_resolver.settings, "VICTORIA_METRICS_URL", "")
        assert await resolve_node_name(dict(_NODE_LABELS)) is None


class TestEmbedRender:
    """Имя ноды должно доехать до самого сообщения, а не осесть в labels."""

    @staticmethod
    def _incident():
        return Incident(
            incident_id="node-sat-1",
            severity="warning",
            status="firing",
            summary="System load per core above 2",
            description="System load per core at 192.168.74.165:9100 above 2.",
            namespace="monitoring",
            labels={**_NODE_LABELS, "node": "dev-14", "severity": "warning"},
            annotations={},
            starts_at="2026-08-13T04:41:00Z",
        )

    async def _embed(self, ctx):
        sent = {}

        async def fake_post(self, url, json=None, **_):
            sent["payload"] = json
            resp = MagicMock()
            resp.status_code = 204
            return resp

        with patch("app.services.discord_service.settings.DISCORD_DRY_RUN", False), \
             patch("app.services.discord_service.settings.DISCORD_WEBHOOK_URL",
                   "https://example.com/wh"), \
             patch("httpx.AsyncClient.post", new=fake_post):
            await DiscordService().send_enriched_alert([ctx], env="dev")
        return sent["payload"]["embeds"][0]

    @pytest.mark.asyncio
    async def test_title_shows_node_not_ip(self):
        ctx = EnrichedContext(incident=self._incident(), node="dev-14")
        embed = await self._embed(ctx)
        assert "dev-14" in embed["title"]
        # Ровно то, что раньше приезжало вместо имени ноды.
        assert "192.168.74.165" not in embed["title"]
        assert "vm-node " not in embed["title"]

    @pytest.mark.asyncio
    async def test_node_field_present(self):
        ctx = EnrichedContext(incident=self._incident(), node="dev-14")
        embed = await self._embed(ctx)
        node_fields = [f for f in embed["fields"] if f["name"] == "Нода"]
        assert len(node_fields) == 1
        assert "dev-14" in node_fields[0]["value"]

    @pytest.mark.asyncio
    async def test_no_node_field_for_regular_alert(self):
        """Обычный алерт лишнего поля не получает."""
        ctx = EnrichedContext(incident=self._incident(), service="auth-service")
        embed = await self._embed(ctx)
        assert not [f for f in embed["fields"] if f["name"] == "Нода"]


class TestPhantomService:
    def test_exporter_pod_gives_no_service(self):
        """Раньше strip давал «vm-node» и он уезжал в заголовок embed'а."""
        ns, svc = _resolve_target_service_from_labels(dict(_NODE_LABELS))
        assert (ns, svc) == ("monitoring", None)

    def test_regular_pod_still_stripped(self):
        """Обычные алерты не задеты — deployment по-прежнему выводится из пода."""
        _, svc = _resolve_target_service_from_labels(
            {"namespace": "prod-shared", "pod": "auth-service-7f8c4b6cdf-h2x9k"},
        )
        assert svc == "auth-service"
