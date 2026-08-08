"""Wave 7-Z: парсер NATS subjects из исходников WO monorepo → kg_*.

Идея
====
В WO микросервисы общаются через NATS JetStream. Сейчас в KG мы знаем только
**факт** зависимости сервиса от NATS-cluster (`uses_nats` → synthetic-узел
`nats-shared` / `nats-kingdom` из `kg_sync._extract_nats_clusters`), но не
знаем какие именно subjects кто публикует и кто читает. Без этого `kg_fragile`
не понимает "Map.Service слегает потому что MapCoordinator не публикует
`leaderboardfinished`".

Этот воркер строит граф `publisher → subject ← subscriber`:

- **Subject** регистрируется как synthetic-Service в namespace `nats-subjects`,
  name=`subject:<value>` (например `subject:march-export`). Это позволяет
  переиспользовать существующие `kg_services`/`kg_service_edges` без новых
  таблиц и миграций.
- **Edge** kind=`uses_nats`, src=сервис, dst=subject-узел.
  `direction` (= `pub` / `sub`) — часть ИДЕНТИЧНОСТИ ребра (колонка +
  UNIQUE(src,dst,kind,direction)): сервис, который И публикует, И читает
  один subject, имеет ДВА ребра. Раньше direction жил только в extras,
  pub+sub схлопывались в одно ребро и направление флипфлопило между
  тиками. `extras.direction` дублируется для обратной совместимости
  консьюмеров. weight = кол-во call-site-ов.

Источник данных
===============
Локальный shallow clone monorepo (по умолчанию
`/var/lib/sre-ai/wo-monorepo`). Clone/fetch in-place при каждом запуске
(см. `_ensure_monorepo`). Sparse-checkout по `GR.WO.*` + `GR.Platform*`.

Регексы покрывают паттерны WO (см. fixtures в tests/kg/fixtures/nats_csharp/):

1. **Subscriber** = класс, унаследованный от
   `NatsJetStreamConsumer<T>` / `NatsJetStreamBatchConsumer<T>` /
   `MapNatsJetStreamConsumer<T>` / `MapNatsJetStreamBatchConsumer<T>`.
   Subject читается из override-а `Subject => NatsSubjectConst.<NAME>` или
   `Subject => "literal-string"`.
2. **Publisher** = `SendToJetStreamAsync(...)` / `PublishAsync(...)` вызов,
   где первым/именованным аргументом `subject:` идёт `NatsSubjectConst.<NAME>`
   или literal-строка.

Resolving `NatsSubjectConst.<NAME>` → "literal" делает один проход по
`GR.Platform/DataBus/Nats/NatsConst.cs` (см. `_load_subject_constants`).

Service-name резолвинг
======================
Путь файла `GR.WO.<X.Y.Z>/...` → service name `<x>-<y>-<z>` (lowercase,
dots-to-dash). Примеры:
- `GR.WO.Map.Service/Consumers/MarchExportConsumer.cs` → `map-service`
- `GR.WO.MapCoordinator.Service/...`                   → `mapcoordinator-service`
- `GR.WO.City.Workers/...`                             → `city-workers`

Это **матчится с именами Deployment-ов в k8s** (см. карту инфры) — поэтому
будущие edges связываются с уже существующими real-services-узлами.

CLI
===
    python -m app.knowledge_graph.nats_subjects_sync           # full sync
    python -m app.knowledge_graph.nats_subjects_sync --dry-run # печатает находки
    python -m app.knowledge_graph.nats_subjects_sync --path /tmp/wo-monorepo
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from app.knowledge_graph.edge_decay_guard import (
    SOURCE_NATS_SUBJECTS_SYNC, record_source_run)
from app.knowledge_graph.populator import upsert_edge, upsert_service
from app.knowledge_graph.schema import NODE_KIND_SERVICE, Service

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Константы / regex-набор
# ---------------------------------------------------------------------------

# Synthetic namespace для subject-узлов. Не пересекается с реальными k8s-ns
# (нет такого `ns` в кластере) → не сматчит реальный Deployment.
NATS_SUBJECTS_NAMESPACE = "nats-subjects"

# Минимальная глубина каталога монорепо (sanity-check на пустой/случайный путь):
# должно быть хотя бы N C#-файлов перед запуском парсинга, иначе abort.
_MIN_CS_FILES = 50

# Subscriber: класс наследник от NatsJetStreamConsumer<T> / -BatchConsumer<T> /
# Map-вариантов. Не критично иметь точный <T> — нам нужны overrides.
# Класс может быть `public sealed class Foo : MapNatsJetStreamConsumer<T>` или
# через primary-constructor `public class Foo(...args) : NatsJetStreamConsumer<T>(...)`.
_CONSUMER_BASE_RE = re.compile(
    r":\s*(?:Map)?Nats(?:JetStream)?(?:Batch)?Consumer\s*<",
)

# Override строк `Subject` / `FilterSubject` / `StreamName` / `ConsumerName`.
# Захватывает либо `NatsSubjectConst.NAME` либо `"literal"`.
# Примеры матча:
#   protected override string Subject       => NatsSubjectConst.MARCH_EXPORT;
#   protected override string Subject => "leaderboardfinished";
#   protected override string Subject{get{return NatsSubjectConst.X;}}    (не покрываем — block-form)
_CONSUMER_SUBJECT_RE = re.compile(
    r"protected\s+override\s+string\s+Subject\s*=>\s*"
    r"(?:NatsSubjectConst\.(?P<const>[A-Z_][A-Z0-9_]*)|\"(?P<lit>[^\"]+)\")",
)
_CONSUMER_FILTER_SUBJECT_RE = re.compile(
    r"protected\s+override\s+string\s+FilterSubject\s*=>\s*"
    r"(?:NatsSubjectConst\.(?P<const>[A-Z_][A-Z0-9_]*)|\"(?P<lit>[^\"]+)\")",
)

# Publisher: `SendToJetStreamAsync` вызов. Subject может быть позиционным
# вторым аргументом или именованным `subject:`. Делаем два паттерна.
# Pattern A (named): ...SendToJetStreamAsync(<...>, subject: NatsSubjectConst.X, ...)
# Pattern B (positional first const): _natsService.SendToJetStreamAsync(NatsConst.SharedRealmId, NatsSubjectConst.LEADERBOARD_REFRESHED, ...)
#   — после realm-id (целая константа) идёт subject как `NatsSubjectConst.X` или строка.
_PUBLISH_NAMED_SUBJECT_RE = re.compile(
    r"SendToJetStreamAsync[^;]{0,400}?\bsubject:\s*"
    r"(?:NatsSubjectConst\.(?P<const>[A-Z_][A-Z0-9_]*)|\"(?P<lit>[^\"]+)\")",
    re.DOTALL,
)
_PUBLISH_POSITIONAL_SUBJECT_RE = re.compile(
    # realmId (variable, NatsConst.SharedRealmId, или integer-literal),
    # comma, subject. Subject — NatsSubjectConst.X | "literal".
    r"SendToJetStreamAsync\s*\(\s*(?:[A-Za-z_][\w\.]*|\d+)\s*,\s*"
    r"(?:NatsSubjectConst\.(?P<const>[A-Z_][A-Z0-9_]*)|\"(?P<lit>[^\"]+)\")",
)

# NatsSubjectConst constant definitions:
#   public const string MARCH_EXPORT = "march-export";
_SUBJECT_CONST_DEF_RE = re.compile(
    r"public\s+const\s+string\s+(?P<name>[A-Z_][A-Z0-9_]*)\s*=\s*\"(?P<value>[^\"]+)\";"
)

# `GR.WO.X.Y` → `x-y` (lowercase, dot-to-dash). Игнорируем `GR.Platform*` /
# `GR.WO.LoadTests` / `GR.WO.*.Tests` — они либо не сервисы, либо тестовые.
_GR_WO_DIR_RE = re.compile(r"^GR\.WO\.([A-Za-z0-9\.]+?)(?:\.Tests)?$")
_GR_WO_EXCLUDE = frozenset({
    "GR.WO.LoadTests",
    "GR.WO.Map.ClientSandbox",      # клиент-side sandbox, не deployment
})

# DLQ subjects шумят (NatsJetStreamConsumer.cs:332 — общий DLQ publish для
# любого consumer). Не хотим засорять граф фиктивным DLQ-subject-ом, который
# вообще не существует в runtime как фиксированный subject. Skip-set:
_SKIP_SUBJECTS: frozenset[str] = frozenset({
    # имена не подходящие как subject (могли матчиться от чего угодно)
})

# Известная разметка: subject `analytics` / `leaderboardfinished` etc. —
# короткие имена. Если константа резолвится к чему-то длиннее 64 символов или
# содержит `{` (форматирование строк) — это явно не fixed subject, skip.
_MAX_SUBJECT_LENGTH = 64

# ---------------------------------------------------------------------------
# Service-name резолвинг
# ---------------------------------------------------------------------------


def _service_name_from_path(rel_path: Path) -> Optional[str]:
    """`GR.WO.Map.Service/Consumers/...` → `map-service`. None если skip."""
    if not rel_path.parts:
        return None
    top = rel_path.parts[0]
    if top in _GR_WO_EXCLUDE:
        return None
    if not top.startswith("GR.WO."):
        return None
    m = _GR_WO_DIR_RE.match(top)
    if not m:
        return None
    raw = m.group(1)
    # GR.WO.Map.Service → map.service → map-service
    return raw.lower().replace(".", "-")


# ---------------------------------------------------------------------------
# Парсинг
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubjectUsage:
    """Одна находка: service публикует или подписан на subject."""
    service: str           # `map-service`
    subject: str           # `march-export`
    direction: str         # `pub` | `sub`
    source_file: str       # relative path (для отладки/audit-log)


@dataclass
class ParseResult:
    usages: List[SubjectUsage] = field(default_factory=list)
    unresolved_constants: Set[str] = field(default_factory=set)
    files_scanned: int = 0
    files_with_findings: int = 0

    @property
    def services(self) -> Set[str]:
        return {u.service for u in self.usages}

    @property
    def subjects(self) -> Set[str]:
        return {u.subject for u in self.usages}


def _load_subject_constants(monorepo_root: Path) -> Dict[str, str]:
    """Найти `NatsConst.cs` и распарсить `public const string X = "y";`.

    Если файла нет — возвращаем пустой dict; константы тогда останутся
    unresolved (заносим в `ParseResult.unresolved_constants`, но не падаем —
    literal-строки в коде всё равно сматчим).
    """
    candidates = list(monorepo_root.glob("**/NatsConst.cs"))
    out: Dict[str, str] = {}
    for c in candidates:
        try:
            text = c.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning("nats_subjects.load_const.read_failed path=%s err=%s", c, e)
            continue
        for m in _SUBJECT_CONST_DEF_RE.finditer(text):
            name = m.group("name")
            value = m.group("value")
            if len(value) > _MAX_SUBJECT_LENGTH or "{" in value:
                continue
            out[name] = value
    logger.info("nats_subjects.const_resolved count=%d", len(out))
    return out


def _resolve_subject(
    const: Optional[str],
    lit: Optional[str],
    constants: Dict[str, str],
    unresolved: Set[str],
) -> Optional[str]:
    if lit:
        return lit if (len(lit) <= _MAX_SUBJECT_LENGTH and "{" not in lit) else None
    if const:
        if const in constants:
            return constants[const]
        unresolved.add(const)
        return None
    return None


def _iter_cs_files(monorepo_root: Path) -> Iterable[Path]:
    """Перебор всех `*.cs` файлов кроме явно тестовых/sandbox-ных."""
    for path in monorepo_root.glob("**/*.cs"):
        rel = path.relative_to(monorepo_root)
        # Skip test-проекты и sandbox
        if any(p.endswith(".Tests") or p == "Tests" for p in rel.parts):
            continue
        if rel.parts and rel.parts[0] in _GR_WO_EXCLUDE:
            continue
        yield path


def parse_monorepo(monorepo_root: Path) -> ParseResult:
    """Сканировать monorepo и собрать SubjectUsage-список."""
    if not monorepo_root.is_dir():
        raise FileNotFoundError(f"monorepo path not found: {monorepo_root}")

    result = ParseResult()
    constants = _load_subject_constants(monorepo_root)

    for cs_path in _iter_cs_files(monorepo_root):
        rel = cs_path.relative_to(monorepo_root)
        result.files_scanned += 1
        try:
            text = cs_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.debug("nats_subjects.read_failed path=%s err=%s", rel, e)
            continue

        found_in_file = parse_csharp_text(
            text=text,
            rel_path=str(rel),
            service_name=_service_name_from_path(rel),
            constants=constants,
            unresolved=result.unresolved_constants,
        )
        if found_in_file:
            result.usages.extend(found_in_file)
            result.files_with_findings += 1

    return result


def parse_csharp_text(
    text: str,
    rel_path: str,
    service_name: Optional[str],
    constants: Dict[str, str],
    unresolved: Set[str],
) -> List[SubjectUsage]:
    """Без I/O: парсит текст файла. Используется и в проде, и в юнит-тестах.

    Если `service_name=None` (не GR.WO.* файл) — всё равно сканируем
    publisher-вызовы (они могут быть в `GR.Platform/DataBus/`), но без
    service-name пропускаем (некому атрибутировать). На subscriber-классы
    в `GR.Platform/` мы тоже не реагируем — реальные consumer'ы наследуют
    их и сами имеют override `Subject` (это и матчим в GR.WO.*).
    """
    if service_name is None:
        return []

    usages: List[SubjectUsage] = []

    # 1. Subscriber: ищем override Subject (один на файл — обычно один
    #    consumer-класс на файл; multi-class файлов в WO почти нет).
    is_consumer = bool(_CONSUMER_BASE_RE.search(text))
    if is_consumer:
        m = _CONSUMER_SUBJECT_RE.search(text)
        if m:
            subj = _resolve_subject(m.group("const"), m.group("lit"), constants, unresolved)
            if subj:
                usages.append(SubjectUsage(
                    service=service_name,
                    subject=subj,
                    direction="sub",
                    source_file=rel_path,
                ))
        else:
            # FilterSubject как fallback (некоторые consumers задают только его)
            m2 = _CONSUMER_FILTER_SUBJECT_RE.search(text)
            if m2:
                subj = _resolve_subject(m2.group("const"), m2.group("lit"), constants, unresolved)
                if subj:
                    usages.append(SubjectUsage(
                        service=service_name,
                        subject=subj,
                        direction="sub",
                        source_file=rel_path,
                    ))

    # 2. Publisher: все callsite-ы SendToJetStreamAsync (named + positional)
    for rx in (_PUBLISH_NAMED_SUBJECT_RE, _PUBLISH_POSITIONAL_SUBJECT_RE):
        for m in rx.finditer(text):
            subj = _resolve_subject(m.group("const"), m.group("lit"), constants, unresolved)
            if subj and subj not in _SKIP_SUBJECTS:
                usages.append(SubjectUsage(
                    service=service_name,
                    subject=subj,
                    direction="pub",
                    source_file=rel_path,
                ))

    return usages


# ---------------------------------------------------------------------------
# Monorepo I/O — clone/fetch in-place
# ---------------------------------------------------------------------------


def _ensure_monorepo(local_path: Path, repo_ssh_url: str, sparse_dirs: List[str]) -> Path:
    """Гарантировать что в `local_path` лежит свежий shallow clone.

    Если каталога нет — `git clone --depth=1 --filter=blob:none --no-checkout`,
    потом `git sparse-checkout init --cone` + `set <dirs>` + `git checkout`.
    Если есть — `git fetch --depth=1` + `git reset --hard origin/<branch>`.
    """
    local_path.mkdir(parents=True, exist_ok=True)
    git_dir = local_path / ".git"

    if not git_dir.exists():
        logger.info("nats_subjects.monorepo_clone path=%s", local_path)
        subprocess.run(
            ["git", "clone", "--depth=1", "--filter=blob:none", "--no-checkout",
             repo_ssh_url, str(local_path)],
            check=True, timeout=300,
        )
        subprocess.run(
            ["git", "-C", str(local_path), "sparse-checkout", "init", "--cone"],
            check=True, timeout=30,
        )
        subprocess.run(
            ["git", "-C", str(local_path), "sparse-checkout", "set", *sparse_dirs],
            check=True, timeout=30,
        )
        subprocess.run(
            ["git", "-C", str(local_path), "checkout", "HEAD"],
            check=True, timeout=120,
        )
    else:
        logger.info("nats_subjects.monorepo_fetch path=%s", local_path)
        # Определяем remote default-branch через `git remote show origin`:
        # вместо этого делаем дешевле — fetch + reset на FETCH_HEAD.
        subprocess.run(
            ["git", "-C", str(local_path), "fetch", "--depth=1", "origin"],
            check=True, timeout=300,
        )
        subprocess.run(
            ["git", "-C", str(local_path), "reset", "--hard", "FETCH_HEAD"],
            check=True, timeout=60,
        )
        # Гарантируем что sparse-checkout dirs не разъехался (если конфиг
        # поменялся в feature-branch — обновим).
        subprocess.run(
            ["git", "-C", str(local_path), "sparse-checkout", "set", *sparse_dirs],
            check=True, timeout=30,
        )
    return local_path


# ---------------------------------------------------------------------------
# KG persist
# ---------------------------------------------------------------------------


def _ensure_subject_node(db: Session, subject: str) -> Service:
    """Получить или создать synthetic `subject:<x>` узел.

    Subject-узлы все живут в одном synthetic-namespace `nats-subjects`
    (см. NATS_SUBJECTS_NAMESPACE). Это избегает per-env дублирования (один
    subject `march-export` существует во ВСЕХ окружениях с одинаковой ролью)
    и держит граф компактным.
    """
    return upsert_service(
        db,
        namespace=NATS_SUBJECTS_NAMESPACE,
        name=f"subject:{subject}",
        metadata={"kind": "nats_subject", "subject": subject},
        synthetic=True,
    )


def persist_to_kg(
    db: Session,
    usages: List[SubjectUsage],
) -> Dict[str, Any]:
    """Положить находки в KG.

    Для каждого (service, subject, direction):
      1. Берём `kg_services` запись сервиса (любой namespace — если сервис
         live в N окружениях, мы хотим один edge per ns × direction).
      2. Upsert synthetic subject-node в `nats-subjects` namespace.
      3. Upsert edge `kind='uses_nats'`, `extras={direction, sources:[..]}`.

    Если сервис ещё не зарегистрирован в KG (kg_sync ещё не наполнил) —
    SKIP, не создаём «вакантный» Service узел в произвольном namespace
    (мы не знаем какой именно).
    """
    stats: Dict[str, Any] = {
        "subjects_upserted": 0,
        "edges_upserted": 0,
        "services_skipped_unknown": 0,
        "total_usages": len(usages),
    }

    # Группировка по (service, subject, direction) → weight.
    grouped: Dict[Tuple[str, str, str], List[str]] = {}
    for u in usages:
        key = (u.service, u.subject, u.direction)
        grouped.setdefault(key, []).append(u.source_file)

    # Кэш subject-узлов и сервисных match-ей.
    subject_nodes: Dict[str, Service] = {}
    svc_lookup_cache: Dict[str, List[Service]] = {}
    seen_unknown: Set[str] = set()

    for (svc_name, subj, direction), sources in grouped.items():
        # Subject-node (один на subject).
        sn = subject_nodes.get(subj)
        if sn is None:
            sn = _ensure_subject_node(db, subj)
            subject_nodes[subj] = sn
            stats["subjects_upserted"] += 1

        # Сервис(ы): один сервис может жить в N namespace (squad-1, squad-2, prod).
        # Мы создаём edge для КАЖДОГО, чтобы per-env запросы видели зависимость.
        services = svc_lookup_cache.get(svc_name)
        if services is None:
            services = (
                db.query(Service)
                .filter(
                    Service.name == svc_name,
                    Service.synthetic.is_(False),
                    Service.node_kind == NODE_KIND_SERVICE,
                )
                .all()
            )
            svc_lookup_cache[svc_name] = services

        if not services:
            if svc_name not in seen_unknown:
                seen_unknown.add(svc_name)
                stats["services_skipped_unknown"] += 1
                logger.info(
                    "nats_subjects.service_unknown name=%s — kg_sync ещё не "
                    "видел этот deployment; edge не создаётся.",
                    svc_name,
                )
            continue

        for svc in services:
            upsert_edge(
                db,
                src=svc,
                dst=sn,
                kind="uses_nats",
                weight=len(sources),
                discovered_by="kg_sync/nats_subjects_parser",
                # direction — часть идентичности ребра: pub и sub одного
                # subject сосуществуют как ДВА ребра (не перезаписывают
                # друг друга). В extras дублируем для консьюмеров,
                # читающих старый формат.
                direction=direction,
                extras={
                    "direction": direction,
                    "subject": subj,
                    "source_files": sources[:5],  # cap, чтобы не дуть JSON
                },
            )
            stats["edges_upserted"] += 1

    db.commit()
    return stats


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def sync_nats_subjects(
    db: Session,
    monorepo_path: Optional[Path] = None,
    skip_fetch: bool = False,
) -> Dict[str, Any]:
    """Полный sync: ensure-clone → parse → persist.

    Параметры берутся из env-vars:
      - `WO_MONOREPO_PATH`         (default `/var/lib/sre-ai/wo-monorepo`)
      - `WO_MONOREPO_SSH_URL`      (default `ssh://git@wo-gitlab.lastoasisgame.com/new-wo/backend-services.git`)
      - `WO_MONOREPO_SPARSE_DIRS`  (default `GR.Platform,GR.WO.*` — comma-sep)

    `skip_fetch=True` — пропустить git clone/fetch, использовать уже
    существующий путь как есть. Используется в CI/manual runs.
    """
    if monorepo_path is None:
        monorepo_path = Path(os.environ.get("WO_MONOREPO_PATH", "/var/lib/sre-ai/wo-monorepo"))
    ssh_url = os.environ.get(
        "WO_MONOREPO_SSH_URL",
        "ssh://git@wo-gitlab.lastoasisgame.com/new-wo/backend-services.git",
    )
    sparse_dirs_raw = os.environ.get("WO_MONOREPO_SPARSE_DIRS", "GR.Platform,GR.Platform.Features,GR.WO.*")
    sparse_dirs = [s.strip() for s in sparse_dirs_raw.split(",") if s.strip()]

    # Каждый выход из функции (в т.ч. аварийный) отчитывается edge-decay
    # guard'у: `uses_nats`-рёбра subject-парсера можно децаить ТОЛЬКО если
    # этот синк реально отработал. Раньше сбой git/clone был не отличим от
    # «в монорепе больше нет NATS-вызовов» и рёбра тихо старели.
    def _report(result: Dict[str, Any]) -> Dict[str, Any]:
        record_source_run(SOURCE_NATS_SUBJECTS_SYNC, result)
        return result

    if not skip_fetch:
        try:
            _ensure_monorepo(monorepo_path, ssh_url, sparse_dirs)
        except subprocess.CalledProcessError as e:
            logger.error("nats_subjects.git_failed cmd=%s rc=%d", e.cmd, e.returncode)
            return _report({"error": "git_failed", "files_scanned": 0})
        except subprocess.TimeoutExpired:
            logger.error("nats_subjects.git_timeout")
            return _report({"error": "git_timeout", "files_scanned": 0})

    # Sanity-check: достаточно ли C#-файлов?
    cs_count = sum(1 for _ in monorepo_path.glob("**/*.cs"))
    if cs_count < _MIN_CS_FILES:
        logger.warning(
            "nats_subjects.too_few_cs path=%s count=%d < %d — abort",
            monorepo_path, cs_count, _MIN_CS_FILES,
        )
        return _report({"error": "too_few_cs_files", "files_scanned": cs_count})

    parsed = parse_monorepo(monorepo_path)
    logger.info(
        "nats_subjects.parsed files=%d findings=%d services=%d subjects=%d unresolved=%d",
        parsed.files_scanned,
        len(parsed.usages),
        len(parsed.services),
        len(parsed.subjects),
        len(parsed.unresolved_constants),
    )

    persisted = persist_to_kg(db, parsed.usages)
    persisted.update({
        "files_scanned": parsed.files_scanned,
        "files_with_findings": parsed.files_with_findings,
        "services_found": len(parsed.services),
        "subjects_found": len(parsed.subjects),
        "unresolved_constants": len(parsed.unresolved_constants),
        "completed_at": datetime.utcnow().isoformat(),
    })
    return _report(persisted)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else "")
    parser.add_argument("--path", default=None, help="Path to monorepo (overrides $WO_MONOREPO_PATH)")
    parser.add_argument("--dry-run", action="store_true", help="Печать находок, без записи в БД")
    parser.add_argument("--skip-fetch", action="store_true", help="Не делать git clone/fetch")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    path = Path(args.path) if args.path else None

    if args.dry_run:
        # Skip DB. Просто запускаем parse + печатаем сводку.
        if path is None:
            path = Path(os.environ.get("WO_MONOREPO_PATH", "/var/lib/sre-ai/wo-monorepo"))
        if not args.skip_fetch:
            ssh_url = os.environ.get(
                "WO_MONOREPO_SSH_URL",
                "ssh://git@wo-gitlab.lastoasisgame.com/new-wo/backend-services.git",
            )
            sparse_dirs_raw = os.environ.get("WO_MONOREPO_SPARSE_DIRS", "GR.Platform,GR.Platform.Features,GR.WO.*")
            sparse_dirs = [s.strip() for s in sparse_dirs_raw.split(",") if s.strip()]
            _ensure_monorepo(path, ssh_url, sparse_dirs)
        parsed = parse_monorepo(path)
        print(f"files_scanned={parsed.files_scanned}")
        print(f"findings={len(parsed.usages)}")
        print(f"services={sorted(parsed.services)}")
        print(f"subjects={sorted(parsed.subjects)}")
        print(f"unresolved_constants={sorted(parsed.unresolved_constants)}")
        for u in parsed.usages[:30]:
            print(f"  {u.service:30s} {u.direction:3s} {u.subject:30s}  ({u.source_file})")
        return 0

    from app.database import SessionLocal
    db = SessionLocal()
    try:
        result = sync_nats_subjects(db, monorepo_path=path, skip_fetch=args.skip_fetch)
        for k, v in result.items():
            print(f"{k}={v}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
