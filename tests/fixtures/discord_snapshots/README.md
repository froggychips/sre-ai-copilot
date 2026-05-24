# Discord alert gallery — known-good snapshots (Gate #23)

7 reference-кейсов из `docs/discord-embeds-preview.html` (локальный preview, в .gitignore), зафиксированные как input/expected JSON fixtures + регрессионные тесты в `tests/test_discord_alert_gallery.py`. Любая будущая правка Wave 7 builder'ов, UX-полировки stats_digest или classifier'а — ловится этими тестами как diff в `expected.json`. UX-regression guard для on-call experience.

## Cases

### `#infra-error` (4)

| # | Файл | Кейс | UX-фокус |
|---|------|------|----------|
| 1 | `01_critical_fresh` | `KubePodCrashLooping · clickhouse-shard0-0`, critical, fresh | Красный цвет, все Wave 7 секции (`🎯 Blast radius`, `📨 NATS impact`, `🕒 Pod trail`), inline row Replicas/Pod/Reason, `🎯 Скорее всего` из PodEventsRule, `⭐ Почему важно` (shared dep), full outgoing_deps с confidence badge |
| 2 | `02_critical_resurfaced` | Тот же alert через 4ч + PATCH-dedup | Footer формата `× 3 в 30мин · first 10:38 · last 14:42`, title содержит ` · 🌀 RESURFACED`. `first`/`last` времена нормализуются на `<TS>` чтобы snapshot был stable между прогонами |
| 3 | `03_warning_compact` | `KubeStatefulSetReplicasMismatch · clickhouse-keeper`, warning | Жёлтый цвет, **БЕЗ Wave 7 секций** (critical-only gate), `🔗 Deps` compact (counts inline), top-1 pod_event без message, без primary_hypothesis (rule_facts пустой) |
| 4 | `04_burst_aggregation` | `KubePodCrashLooping`, 8 pods одного deployment | AM batch'ит N алертов в один webhook payload; `send_enriched_alert` collapses в один embed (head = contexts[0]). Single ns → нет `(N ns)` суффикса в title |

### `#infra-stats` (3)

| # | Файл | Кейс | UX-фокус |
|---|------|------|----------|
| 5 | `05_daily_digest` | Daily digest markdown (item #1 + #2) | `firing_alerts_section` рендерит `series` (не cyrillic «с»). `unowned_namespaces_section` рендерит top-N + multi-signal suggest (`**@platform**` для high-confidence, `@?` для пустого, manual-suffix) |
| 6 | `06_chronic_digest` | Chronic digest 4×/день | `_format(rows, 24h)` — markdown с fires count, firing-duration `Nh`, quiet `Mm назад`, truncation marker `(+K ещё)` после 15 строк. `now()` запатчен на фикстуру для detrministic-output |
| 7 | `07_team_digest` | Per-team daily digest (squad-7) | `render_embed(digest)` — full embed: 6 fields. Color picker (critical/warning/ok/neutral) по severity-breakdown. Bar-шкала health_score (`●○○`/`●●○`/`●●●`). Stuck-section с severity-emoji + hours_firing |

## Запуск

```bash
# Прогон всех 7 cases (default — assert equality):
.venv/bin/pytest tests/test_discord_alert_gallery.py -x

# Прогон одного case'а:
.venv/bin/pytest tests/test_discord_alert_gallery.py -k 02_critical_resurfaced -v
```

## Workflow: update snapshot при намеренной UX-правке

При **намеренной** правке builder'а, которая меняет embed (например, добавили новое поле, переименовали секцию):

```bash
# 1. Запустить с UPDATE_SNAPSHOTS=1 — перезаписать expected.json:
UPDATE_SNAPSHOTS=1 .venv/bin/pytest tests/test_discord_alert_gallery.py

# 2. Посмотреть git diff — убедиться что только нужные поля изменились:
git diff tests/fixtures/discord_snapshots/

# 3. Закоммитить и input/expected, и сам change builder'а одним PR.
# Reviewer видит "+/-N полей в N embeds" — обзор UX-изменения в одном месте.
```

**НЕ запускать `UPDATE_SNAPSHOTS=1` локально без diff**: он скрывает unintended regressions. Auto-обновление есть только когда `expected.json` отсутствует (первый прогон case'а).

## Стабильность snapshot'ов

Нормализация в `_normalize_embed` убирает:
- `embed.timestamp` — `datetime.now(timezone.utc).isoformat()` на момент send.
- `footer.text · first HH:MM · last HH:MM` (от dedup PATCH) — заменяется на `<TS>`.

Любые другие dynamic-поля → snapshot нестабилен → исправлять в builder'е или расширять normalizer.

## Что НЕ покрывает gate #23

- Полный `build_digest` (нужен VM + SQLAlchemy session) — покрыто `test_stats_digest*.py` отдельно.
- Реальный PATCH-flow `_patch_enriched_recurrence` — покрыт `test_enriched_alert_dedup.py`; здесь только форма footer-а.
- Severity gate `_should_route_to_error` (info/none → drop) — `test_alert_enrichment.py`.

Gate #23 — UX-shape, не функциональная корректность.
