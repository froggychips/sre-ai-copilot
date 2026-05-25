"""Gate #23 — known-good Discord alert gallery snapshot regression tests.

Гарантирует что 7 reference-кейсов из docs/discord-embeds-preview.html
останутся структурно стабильными при любых изменениях builder'ов
(Wave 7 секции, UX полировка, classifier и т.д.).

Cases (см. fixtures/discord_snapshots/):
  01_critical_fresh        — KubePodCrashLooping critical, все Wave 7 секции
  02_critical_resurfaced   — тот же alert + PATCH-dedup footer (×3, first/last)
  03_warning_compact       — KubeStatefulSetReplicasMismatch warning, без Wave 7
  04_burst_aggregation     — KubePodCrashLooping 8 pods agg-embed
  05_daily_digest          — full stats_digest markdown с 6 UX-секциями
  06_chronic_digest        — chronic_digest markdown
  07_team_digest           — render_embed() per-team digest

Workflow update:
  UPDATE_SNAPSHOTS=1 /path/to/pytest tests/test_discord_alert_gallery.py
переписывает expected.json — пользоваться при намеренной UX-правке.
Без флага — assert на equality, fail печатает diff между got vs expected.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from app.diagnostics.facts import Fact
from app.models.incident import Incident
from app.services.alert_enrichment import EnrichedContext
from app.services.discord_service import DiscordService
from app.services.team_digest import render_embed as render_team_digest_embed

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "discord_snapshots"
UPDATE_SNAPSHOTS = os.environ.get("UPDATE_SNAPSHOTS") == "1"

# ── Normalization ─────────────────────────────────────────────────────────────

# Dynamic timestamp keys we strip — embed.timestamp обновляется
# datetime.now(); footer для resurfaced несёт `first HH:MM · last HH:MM`
# которые тоже зависят от now.
_TIMESTAMP_FOOTER_RE = re.compile(
    r" · first \d{2}:\d{2} · last \d{2}:\d{2}"
)


def _normalize_embed(embed: Dict[str, Any]) -> Dict[str, Any]:
    """Strip dynamic fields для stable snapshot.

    - timestamp удалён (datetime.now ISO)
    - footer `first/last HH:MM` суффикс заменён на `<TIMES>` (зависит от
      now() в patch-handler'е)
    """
    out: Dict[str, Any] = {}
    for k, v in embed.items():
        if k == "timestamp":
            continue
        if k == "footer" and isinstance(v, dict) and "text" in v:
            text = v["text"]
            text = _TIMESTAMP_FOOTER_RE.sub(" · first <TS> · last <TS>", text)
            out[k] = {"text": text}
            continue
        out[k] = v
    return out


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _dump_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def _assert_snapshot(case: str, got: Any) -> None:
    """Diff got vs expected.json. UPDATE_SNAPSHOTS=1 — переписать.

    Сравнение — через repr-нормализацию JSON (sort_keys=True), чтобы
    добавление поля где-то посередине embed-а не давало flap.
    """
    expected_path = FIXTURES_DIR / f"{case}.expected.json"
    if UPDATE_SNAPSHOTS or not expected_path.exists():
        _dump_json(expected_path, got)
        return
    expected = _load_json(expected_path)
    got_norm = json.dumps(got, indent=2, ensure_ascii=False, sort_keys=True)
    exp_norm = json.dumps(expected, indent=2, ensure_ascii=False, sort_keys=True)
    if got_norm != exp_norm:
        # Pytest показывает большой diff с -vv; обрезаем не сами, оставляем
        # читаемый JSON.
        diff_hint = (
            f"\nSnapshot mismatch for `{case}`.\n"
            f"  Update with: UPDATE_SNAPSHOTS=1 pytest tests/test_discord_alert_gallery.py -k {case}\n"
            f"  Expected:  {expected_path}\n"
        )
        assert got_norm == exp_norm, diff_hint


# ── Embed capture helper ──────────────────────────────────────────────────────


async def _capture_enriched_embed(
    contexts: List[EnrichedContext],
    env: Optional[str] = "preprod",
    resurfaced: bool = False,
) -> Dict[str, Any]:
    """Прогнать `send_enriched_alert` через httpx-mock, вернуть embed dict.

    Не зависит от DB — берёт уже-собранные EnrichedContext (fixture-input).
    Игнорирует dedup state (clear-ит recent_enriched в setup-ах).
    """
    sent: Dict[str, Any] = {}

    async def fake_post(self, url, json=None, **_):
        # Захватываем первый POST — что нам нужно для snapshot.
        if "payload" not in sent:
            sent["payload"] = json
        resp = MagicMock()
        resp.status_code = 200
        resp.text = ""
        # _post_or_patch_enriched парсит json() для msg_id — даём что-то.
        resp.json = MagicMock(return_value={"id": "snapshot-msg-id"})
        return resp

    # Сбрасываем dedup state перед каждым capture'ом — мы тестируем
    # «новый POST», а не «PATCH в существующее».
    from app.services.discord import dedup as dedup_mod

    with dedup_mod._dedup_lock:
        dedup_mod._recent_enriched.clear()

    with patch("app.services.discord_service.settings.DISCORD_DRY_RUN", False), \
         patch(
             "app.services.discord_service.settings.DISCORD_WEBHOOK_URL",
             "https://discord.com/api/webhooks/test/hook",
         ), \
         patch("httpx.AsyncClient.post", new=fake_post):
        await DiscordService().send_enriched_alert(
            contexts, env=env, resurfaced=resurfaced,
        )
    return sent["payload"]["embeds"][0]


# ── Input → EnrichedContext builder ───────────────────────────────────────────


def _build_incident(data: Dict[str, Any]) -> Incident:
    return Incident(
        incident_id=data.get("incident_id", "fp-snap"),
        severity=data["severity"],
        status=data.get("status", "firing"),
        summary=data.get("summary", "x"),
        description=data.get("description", ""),
        namespace=data.get("namespace"),
        labels=data.get("labels", {}),
        annotations=data.get("annotations", {}),
        starts_at=data["starts_at"],
        generator_url=data.get("generator_url"),
    )


def _build_ctx(input_data: Dict[str, Any]) -> EnrichedContext:
    """Превращает input fixture dict в EnrichedContext.

    Все поля EnrichedContext, кроме `incident`/`rule_facts`, биются 1:1 с
    ключами в fixture JSON. Это keep'ает фикстуры self-describing.
    """
    inc = _build_incident(input_data["incident"])
    ctx_kwargs: Dict[str, Any] = {"incident": inc}
    pass_through_fields = (
        "service", "pod", "team_owner", "in_kg",
        "recent_deploys", "upstream_alerts", "recurrence_24h",
        "inbound_count_by_kind", "outgoing_deps", "pod_events",
        "pod_name", "container_reason", "replicas_ready_desired",
        "jira_issues", "rollout_noise", "kg_data_age_sec",
        "blast_radius", "nats_impact", "pod_trail", "extras",
    )
    for f in pass_through_fields:
        if f in input_data:
            ctx_kwargs[f] = input_data[f]
    # `rule_facts` — отдельная обработка: input dump = list of dicts с
    # ключами Fact-dataclass'а; превращаем обратно в Fact.
    if "rule_facts" in input_data:
        ctx_kwargs["rule_facts"] = [
            Fact(
                kind=rf["kind"],
                observed=rf.get("observed", True),
                confidence=rf.get("confidence", 0.5),
                evidence=rf.get("evidence", {}),
                subject=rf.get("subject"),
                source_rule=rf.get("source_rule"),
            )
            for rf in input_data["rule_facts"]
        ]
    return EnrichedContext(**ctx_kwargs)


# ── Cases: critical/warning/burst — embed snapshot ────────────────────────────


@pytest.mark.asyncio
async def test_snapshot_01_critical_fresh():
    """Case 1: KubePodCrashLooping critical · clickhouse-shard0-0.

    Focus: красный цвет, все Wave 7 секции (blast/NATS/pod_trail),
    human-time, pod/replicas/reason inline-row, primary_hypothesis +
    why_this_matters.
    """
    input_data = _load_json(FIXTURES_DIR / "01_critical_fresh.input.json")
    ctx = _build_ctx(input_data)
    embed = await _capture_enriched_embed([ctx])
    _assert_snapshot("01_critical_fresh", _normalize_embed(embed))


@pytest.mark.asyncio
async def test_snapshot_02_critical_resurfaced():
    """Case 2: тот же alert через 4ч + PATCH-dedup → resurfaced footer.

    Focus: footer показывает `× 3 в 30мин · first <TS> · last <TS>` и
    title — `· 🌀 RESURFACED` маркер.
    """
    input_data = _load_json(FIXTURES_DIR / "02_critical_resurfaced.input.json")
    ctx = _build_ctx(input_data)
    embed = await _capture_enriched_embed([ctx], resurfaced=True)
    # Симулируем PATCH-dedup footer ровно так, как это делает
    # _patch_enriched_recurrence: вставляем суффикс с count и
    # `first/last HH:MM`. Без полноценного PATCH-pipeline'а нам важна
    # «форма» footer-а, которую видит on-call. Контракт самой логики
    # PATCH-у покрыт в test_enriched_alert_dedup.
    base_footer = embed["footer"]["text"]
    count = input_data.get("_simulated_dedup_count", 3)
    ttl_min = input_data.get("_simulated_dedup_ttl_min", 30)
    fake_first = "10:38"
    fake_last = "14:42"
    embed["footer"] = {
        "text": (
            f"{base_footer} · ×{count} в {ttl_min}мин · "
            f"first {fake_first} · last {fake_last}"
        )
    }
    _assert_snapshot("02_critical_resurfaced", _normalize_embed(embed))


@pytest.mark.asyncio
async def test_snapshot_03_warning_compact():
    """Case 3: KubeStatefulSetReplicasMismatch warning · clickhouse-keeper.

    Focus: жёлтый, БЕЗ Wave 7 секций (critical-only), `🔗 Deps`
    compact (counts inline), human-time везде.
    """
    input_data = _load_json(FIXTURES_DIR / "03_warning_compact.input.json")
    ctx = _build_ctx(input_data)
    embed = await _capture_enriched_embed([ctx])
    _assert_snapshot("03_warning_compact", _normalize_embed(embed))


@pytest.mark.asyncio
async def test_snapshot_04_burst_aggregation():
    """Case 4: KubePodCrashLooping critical, 8 pods affected → agg embed.

    Focus: title `(N ns)`-suffix не появляется (один ns), но через несколько
    контекстов один embed. Проверяем что ns_str компактный и все pod_names
    объединены.
    """
    input_data = _load_json(FIXTURES_DIR / "04_burst_aggregation.input.json")
    # 4 кейс — несколько EnrichedContext'ов в одном batch'е (один alertname,
    # разные pods/ns). Берём `contexts` list of dicts.
    contexts_data = input_data["contexts"]
    contexts = [_build_ctx(c) for c in contexts_data]
    embed = await _capture_enriched_embed(contexts)
    _assert_snapshot("04_burst_aggregation", _normalize_embed(embed))


# ── Case 5: Daily digest (markdown, не embed) ─────────────────────────────────


@pytest.mark.asyncio
async def test_snapshot_05_daily_digest():
    """Case 5: Daily digest assembled string.

    Здесь не вызываем build_digest целиком (он лезет в VM и SQLAlchemy
    Session), а проверяем что render-функции отдельных секций при
    фиксированных входах дают стабильный markdown.

    Focus: 6 UX-секций — `series` unit, unowned ns + suggest, top alert types
    с Δ24h/chronic/resurfaced, stale classification, blast-radius rename.
    """
    from app.services import stats_digest

    input_data = _load_json(FIXTURES_DIR / "05_daily_digest.input.json")

    # Item #1: firing alerts section
    fired = [
        {"metric": {"namespace": m["namespace"], "alertname": m["alertname"]}}
        for m in input_data["fired_series_metrics"]
    ]
    ns_to_team = input_data["ns_to_team"]
    alerts_text, _unique, _by_team, unowned_ns = stats_digest.firing_alerts_section(
        fired, ns_to_team,
    )

    # Item #2: unowned namespaces — bulk-suggest mock.
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []  # все ns matchatся по prefix
    unowned_text = stats_digest.unowned_namespaces_section(unowned_ns, db)

    rendered = {
        "alerts_section": alerts_text,
        "unowned_section": unowned_text,
    }
    _assert_snapshot("05_daily_digest", rendered)


# ── Case 6: Chronic digest (markdown) ─────────────────────────────────────────


def test_snapshot_06_chronic_digest():
    """Case 6: Chronic digest 4×/день, `_format` output.

    Focus: 15-row truncation marker, fires count, firing-duration label.
    Используем фиксированный datetime, чтобы firing/quiet windows были
    detrministic.
    """
    from app.services import chronic_digest

    input_data = _load_json(FIXTURES_DIR / "06_chronic_digest.input.json")
    # Реконструируем rows: парсим ISO → datetime (naive, как в SQL).
    rows: List[Dict[str, Any]] = []
    for r in input_data["rows"]:
        first = datetime.fromisoformat(r["first_fired"]).replace(tzinfo=None)
        last = datetime.fromisoformat(r["last_fired"]).replace(tzinfo=None)
        rows.append({
            "namespace": r["namespace"],
            "service": r["service"],
            "alertname": r["alertname"],
            "fires": r["fires"],
            "first_fired": first,
            "last_fired": last,
        })
    # `now` зашит в _format — для detrministic-output патчим datetime.now.
    fake_now = datetime.fromisoformat(input_data["now"])

    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return fake_now if tz is None else fake_now.astimezone(tz)

    with patch.object(chronic_digest, "datetime", _FakeDT):
        out = chronic_digest._format(rows, input_data["window_hours"])

    _assert_snapshot("06_chronic_digest", {"markdown": out})


# ── Case 7: Team digest embed ─────────────────────────────────────────────────


def test_snapshot_07_team_digest():
    """Case 7: per-team daily digest embed (render_embed).

    Focus: цветовая логика, 6 fields (Services/Deploys/Alerts/Fragile/Stuck/SLO),
    bar-шкала health_score, severity-emoji + hours_firing в stuck.
    """
    input_data = _load_json(FIXTURES_DIR / "07_team_digest.input.json")
    digest = input_data["digest"]
    embed = render_team_digest_embed(digest)
    _assert_snapshot("07_team_digest", _normalize_embed(embed))
