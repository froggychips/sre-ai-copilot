"""Тесты на поле «Стенды на ноде» в нодовых алертах.

Запрос дежурного 23.09.2026: `NodeMemoryWillExhaustSoon` на dev-26 говорил
«где» (нода), но не «чьё» — какие стенды там сидят, приходилось смотреть в
kubectl. Теперь enrichment берёт live-список подов ноды, а embed показывает
namespace'ы, склеенные по стенду.

Покрывает:
  - склейку squad-N-* в один стенд, сортировку по числу подов, склонение;
  - свёртку системных namespace'ов в счётчик и случай «только системные»;
  - «нет данных» при сбое API вместо ложного «пусто»;
  - лимит поля Discord 1024 без потери счётчика системных;
  - фильтр завершённых подов и поведение fetch при ошибке API;
  - enrichment: запрос только для нодового алерта, kill-switch, source_status.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.context import deployments
from app.models.incident import Incident
from app.services.alert_enrichment import EnrichedContext, enrich_alert
from app.services.discord.embed_builder import _build_node_namespaces_field, _pods_word
from app.services.discord_service import DiscordService


def _ns(namespace, pods, system=False):
    return {"namespace": namespace, "pods": pods, "system": system}


# Снимок dev-26 на 23.09.2026: squad-38 и шесть системных DaemonSet-ns.
_DEV26 = [
    _ns("squad-38-shared", 38),
    _ns("squad-38-kingdom7", 33),
    _ns("kube-system", 2, True),
    _ns("cattle-system", 1, True),
    _ns("jupyter", 1, True),
    _ns("logging", 1, True),
    _ns("metallb-system", 1, True),
    _ns("monitoring", 1, True),
]


class TestPodsWord:
    @pytest.mark.parametrize("n,expected", [
        (1, "1 под"), (2, "2 пода"), (4, "4 пода"), (5, "5 подов"),
        (11, "11 подов"), (12, "12 подов"), (21, "21 под"), (22, "22 пода"),
        (71, "71 под"), (111, "111 подов"),
    ])
    def test_forms(self, n, expected):
        assert _pods_word(n) == expected


class TestBuildField:
    def test_squad_namespaces_glued_into_stand(self):
        field = _build_node_namespaces_field(_DEV26)
        assert field["name"] == "Стенды на ноде"
        assert field["value"].splitlines()[0] == "`squad-38` — 71 под (kingdom7, shared)"

    def test_system_namespaces_collapsed(self):
        value = _build_node_namespaces_field(_DEV26)["value"]
        assert value.splitlines()[-1] == "+ системные: 6 ns, 7 подов"
        assert "kube-system" not in value

    def test_stands_sorted_by_pods_and_plain_ns_kept(self):
        value = _build_node_namespaces_field([
            _ns("squad-10-shared", 20),
            _ns("preprod-kingdom5", 12),
            _ns("squad-10-kingdom5", 30),
            _ns("squad-31-shared", 3),
        ])["value"]
        assert value.splitlines() == [
            "`squad-10` — 50 подов (kingdom5, shared)",
            "`preprod-kingdom5` — 12 подов",
            "`squad-31` — 3 пода (shared)",
        ]

    def test_only_system_pods(self):
        field = _build_node_namespaces_field([_ns("kube-system", 2, True)])
        assert field["value"] == "стендов нет, только системные: 1 ns, 2 пода"

    def test_empty_node(self):
        assert _build_node_namespaces_field([])["value"] == "_на ноде нет подов_"

    def test_unknown_says_no_data(self):
        """Сбой API — это «не знаю», а не «на ноде пусто»."""
        field = _build_node_namespaces_field(None, "k8s API не ответил")
        assert field["value"] == "_нет данных: k8s API не ответил_"

    def test_not_a_node_alert(self):
        assert _build_node_namespaces_field(None) is None

    def test_fits_discord_limit_and_keeps_system_counter(self):
        many = [_ns(f"squad-{i}-kingdom-with-a-long-name-{i}", 100 - i) for i in range(60)]
        value = _build_node_namespaces_field(many + [_ns("kube-system", 2, True)])["value"]
        assert len(value) <= 1024
        lines = value.splitlines()
        assert lines[0].startswith("`squad-0`")
        assert lines[-2].startswith("… ещё ") and lines[-2].endswith(" стенд.")
        assert lines[-1] == "+ системные: 1 ns, 2 пода"


def _pod(namespace, phase="Running"):
    return SimpleNamespace(
        metadata=SimpleNamespace(namespace=namespace),
        status=SimpleNamespace(phase=phase),
    )


class TestFetch:
    def test_counts_running_pods_and_marks_system(self, monkeypatch):
        monkeypatch.setattr(deployments, "_load_k8s_once", lambda: True)
        api = MagicMock()
        api.list_pod_for_all_namespaces.return_value = SimpleNamespace(items=[
            _pod("squad-38-shared"), _pod("squad-38-shared"),
            _pod("squad-38-shared", "Succeeded"),  # отработавшая миграция
            _pod("squad-38-kingdom7", "Failed"),
            _pod("squad-38-kingdom7", "Pending"),
            _pod("kube-system"),
        ])
        with patch.object(deployments.client, "CoreV1Api", return_value=api):
            result = deployments.fetch_node_namespaces("dev-26", timeout_sec=1.5)
        assert result == [
            {"namespace": "squad-38-shared", "pods": 2, "system": False},
            {"namespace": "kube-system", "pods": 1, "system": True},
            {"namespace": "squad-38-kingdom7", "pods": 1, "system": False},
        ]
        kwargs = api.list_pod_for_all_namespaces.call_args.kwargs
        assert kwargs["field_selector"] == "spec.nodeName=dev-26"
        assert kwargs["_request_timeout"] == 1.5

    def test_api_error_is_none_not_raise(self, monkeypatch):
        monkeypatch.setattr(deployments, "_load_k8s_once", lambda: True)
        api = MagicMock()
        api.list_pod_for_all_namespaces.side_effect = TimeoutError()
        with patch.object(deployments.client, "CoreV1Api", return_value=api):
            assert deployments.fetch_node_namespaces("dev-26") is None

    def test_no_kubeconfig(self, monkeypatch):
        monkeypatch.setattr(deployments, "_load_k8s_once", lambda: False)
        assert deployments.fetch_node_namespaces("dev-26") is None


_NODE_LABELS = {
    "alertname": "NodeMemoryWillExhaustSoon",
    "instance": "192.168.90.26:9100",
    "namespace": "monitoring",
    "pod": "vm-node-exporter-j64pp",
    "service": "vm-node-exporter",
    "severity": "warning",
}


def _incident(labels):
    return Incident(
        incident_id="node-mem-1",
        severity="warning",
        status="firing",
        summary="Memory will exhaust soon",
        description="Прогнозируется исчерпание available memory на узле dev-26.",
        namespace="monitoring",
        labels=labels,
        annotations={},
        starts_at="2026-09-23T12:22:00Z",
    )


def _db():
    db = MagicMock()
    db.query.return_value.filter.return_value.filter.return_value.first.return_value = None
    db.query.return_value.filter.return_value.first.return_value = None
    return db


class TestEnrichment:
    def test_node_alert_gets_namespaces(self):
        with patch("app.context.deployments.fetch_node_namespaces",
                   return_value=_DEV26) as fetch:
            ctx = enrich_alert(_db(), _incident({**_NODE_LABELS, "node": "dev-26"}))
        fetch.assert_called_once()
        assert fetch.call_args.args == ("dev-26",)
        assert ctx.node_namespaces == _DEV26
        assert "node_namespaces" not in ctx.source_status

    def test_api_failure_marked_unknown(self):
        with patch("app.context.deployments.fetch_node_namespaces", return_value=None):
            ctx = enrich_alert(_db(), _incident({**_NODE_LABELS, "node": "dev-26"}))
        assert ctx.node_namespaces is None
        assert ctx.source_status["node_namespaces"] == "k8s API не ответил"

    def test_regular_alert_does_not_ask(self):
        with patch("app.context.deployments.fetch_node_namespaces") as fetch:
            enrich_alert(_db(), _incident(dict(_NODE_LABELS)))
        fetch.assert_not_called()

    def test_kill_switch(self, monkeypatch):
        from app.services import alert_enrichment
        monkeypatch.setattr(alert_enrichment.settings, "ENRICH_NODE_NAMESPACES_ENABLED", False)
        with patch("app.context.deployments.fetch_node_namespaces") as fetch:
            ctx = enrich_alert(_db(), _incident({**_NODE_LABELS, "node": "dev-26"}))
        fetch.assert_not_called()
        assert "node_namespaces" not in ctx.source_status


class TestEmbed:
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
    async def test_field_rendered_for_node_alert(self):
        ctx = EnrichedContext(
            incident=_incident({**_NODE_LABELS, "node": "dev-26"}),
            node="dev-26", node_namespaces=_DEV26,
        )
        embed = await self._embed(ctx)
        fields = [f for f in embed["fields"] if f["name"] == "Стенды на ноде"]
        assert len(fields) == 1
        assert "`squad-38` — 71 под" in fields[0]["value"]

    @pytest.mark.asyncio
    async def test_unknown_rendered_as_no_data(self):
        ctx = EnrichedContext(
            incident=_incident({**_NODE_LABELS, "node": "dev-26"}), node="dev-26",
        )
        ctx.source_status["node_namespaces"] = "k8s API не ответил"
        embed = await self._embed(ctx)
        fields = [f for f in embed["fields"] if f["name"] == "Стенды на ноде"]
        assert fields and "нет данных" in fields[0]["value"]

    @pytest.mark.asyncio
    async def test_no_field_for_regular_alert(self):
        ctx = EnrichedContext(incident=_incident(dict(_NODE_LABELS)), service="auth-service")
        embed = await self._embed(ctx)
        assert not [f for f in embed["fields"] if f["name"] == "Стенды на ноде"]
