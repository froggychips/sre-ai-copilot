#!/usr/bin/env python3
"""Разложить `sre-ai-secrets` по ролям и сверить флаги с ConfigMap — без вывода значений.

Зачем. До 18.11.2026 все пять Deployment-ов подключали один Secret целиком:
executor, единственный процесс с правом записи в кластер, получал пароль
ClickHouse и токены GitLab/Jira/TeamCity, которые его код не читает, а флаги
поведения («мост включён», «какой LLM-бэкенд») лежали там же ключами и в git не
попадали. Теперь флаги — в ConfigMap `sre-ai-copilot-settings`
(k8s/base/settings.yaml), секреты — по ролям (k8s/base/secrets.*.yaml.example).

Скрипт выполняет человек с доступом к кластеру; в CI он не запускается.
ЗНАЧЕНИЯ СЕКРЕТОВ НЕ ПЕЧАТАЮТСЯ НИКОГДА — только имена ключей. Значения
проходят через память процесса и уходят в `kubectl apply -f -` по stdin.

Команды (порядок = порядок миграции, см. docs/RUNBOOK.ru.md):

    plan [роль ...]     что будет скопировано в Secret каждой роли (по умолчанию)
    apply [роль ...]    создать/обновить Secret роли из исходного
    verify              ключи ConfigMap, которые ещё лежат в Secret-ах:
                        совпадают значения или нет (код 1 при расхождении)
    cleanup             напечатать команду, удаляющую из исходного Secret
                        ключи, которые уже есть в ConfigMap С ТЕМ ЖЕ значением
                        (и команду отката); сам ничего не удаляет

Роли и состав ключей берутся из `k8s/base/secrets.<роль>.yaml.example`:
ключ с комментарием `# optional` копируется, только если есть в источнике.

Зависимости — только stdlib и kubectl: скрипт запускают с ноутбука оператора,
где виртуального окружения проекта может не быть.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

REPO = Path(__file__).resolve().parent.parent
BASE_DIR = REPO / "k8s" / "base"
SETTINGS_FILE = BASE_DIR / "settings.yaml"

#: Роли, для которых Secret собирается копированием из исходного. `app` сюда не
#: входит: это и есть исходный `sre-ai-secrets`, его шаблон — справочный.
ROLES = ("executor", "beat")

#: Ключи, которые в исходном Secret есть, в ConfigMap — нет, и удалять их надо
#: тоже: ENV задаётся литералом в pod-спеке (k8s/base/settings.yaml, шапка).
EXTRA_CLEANUP_KEYS = ("ENV",)

_KEY_LINE = re.compile(r"^  (?P<key>[A-Z][A-Z0-9_]*):\s*(?P<rest>.*)$")
_NAME_LINE = re.compile(r"^  name:\s*(?P<name>[a-z0-9.-]+)\s*$")


def parse_example(text: str) -> Tuple[str, List[str], List[str]]:
    """→ (имя Secret, обязательные ключи, необязательные ключи) из шаблона.

    Разбор построчный, без yaml: формат шаблонов фиксирован, а зависимость от
    PyYAML сделала бы скрипт непригодным вне окружения проекта.
    """
    name = ""
    required: List[str] = []
    optional: List[str] = []
    section = ""
    for line in text.splitlines():
        if line and not line.startswith((" ", "#")):
            section = line.split(":", 1)[0]
            continue
        if section == "metadata":
            m = _NAME_LINE.match(line)
            if m:
                name = m.group("name")
        elif section == "stringData":
            m = _KEY_LINE.match(line)
            if m:
                target = optional if "# optional" in m.group("rest") else required
                target.append(m.group("key"))
    if not name or not required:
        raise ValueError("шаблон без metadata.name или без обязательных ключей")
    return name, required, optional


def parse_configmap_data(text: str) -> Dict[str, str]:
    """`data:` из k8s/base/settings.yaml → {ключ: значение}. Значения — строки в
    двойных кавычках (правило файла; test_settings_configmap.py его держит)."""
    out: Dict[str, str] = {}
    in_data = False
    for line in text.splitlines():
        if line and not line.startswith((" ", "#")):
            in_data = line.startswith("data:")
            continue
        if not in_data:
            continue
        m = _KEY_LINE.match(line)
        if not m:
            continue
        rest = m.group("rest").split(" #", 1)[0].strip()
        if len(rest) >= 2 and rest[0] == rest[-1] == '"':
            rest = rest[1:-1]
        out[m.group("key")] = rest
    return out


def build_role_secret(
    source_data: Dict[str, str], name: str, required: Iterable[str], optional: Iterable[str]
) -> Tuple[Dict[str, object], List[str], List[str]]:
    """→ (манифест Secret, скопированные ключи, отсутствующие ОБЯЗАТЕЛЬНЫЕ).

    `source_data` — `.data` исходного Secret (base64 как есть: перекодировать
    незачем, и значение ни разу не становится открытым текстом).
    """
    data: Dict[str, str] = {}
    missing: List[str] = []
    for key in required:
        if key in source_data:
            data[key] = source_data[key]
        else:
            missing.append(key)
    for key in optional:
        if key in source_data:
            data[key] = source_data[key]
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "labels": {"app.kubernetes.io/part-of": "sre-ai-copilot"},
        },
        "type": "Opaque",
        "data": data,
    }
    return manifest, sorted(data), missing


def compare_settings(
    settings: Dict[str, str], secret_data: Dict[str, str]
) -> Tuple[List[str], List[str]]:
    """→ (ключи с ТЕМ ЖЕ значением в Secret, ключи с ДРУГИМ значением).

    Сравнение без учёта регистра и крайних пробелов: для Settings `True` и
    `true` — одно и то же, а хвостовой перевод строки в Secret — частый след
    `--from-file`.
    """
    same: List[str] = []
    differ: List[str] = []
    for key, value in sorted(settings.items()):
        if key not in secret_data:
            continue
        try:
            live = base64.b64decode(secret_data[key]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            differ.append(key)
            continue
        if live.strip().lower() == value.strip().lower():
            same.append(key)
        else:
            differ.append(key)
    return same, differ


def cleanup_patch(keys: Iterable[str]) -> str:
    """JSON-patch, удаляющий ключи из `.data` Secret."""
    return json.dumps([{"op": "remove", "path": f"/data/{k}"} for k in keys])


# ── kubectl ──────────────────────────────────────────────────────────────────


def _secret_data(namespace: str, name: str) -> Dict[str, str] | None:
    res = subprocess.run(
        ["kubectl", "-n", namespace, "get", "secret", name, "-o", "json"],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        return None
    return dict(json.loads(res.stdout).get("data") or {})


def _role_spec(role: str) -> Tuple[str, List[str], List[str]]:
    path = BASE_DIR / f"secrets.{role}.yaml.example"
    return parse_example(path.read_text(encoding="utf-8"))


def cmd_plan_apply(args: argparse.Namespace, do_apply: bool) -> int:
    source = _secret_data(args.namespace, args.source)
    if source is None:
        print(f"ERROR: Secret {args.source} не найден в ns {args.namespace}", file=sys.stderr)
        return 1
    rc = 0
    for role in args.roles or ROLES:
        name, required, optional = _role_spec(role)
        manifest, copied, missing = build_role_secret(source, name, required, optional)
        print(f"{name} ← {args.source}")
        print(f"  ключи: {', '.join(copied) or '—'}")
        skipped = [k for k in optional if k not in copied]
        if skipped:
            print(f"  нет в источнике (необязательные): {', '.join(skipped)}")
        if missing:
            print(f"  ERROR: нет обязательных: {', '.join(missing)}", file=sys.stderr)
            rc = 1
            continue
        if not do_apply:
            continue
        res = subprocess.run(
            ["kubectl", "-n", args.namespace, "apply", "-f", "-"],
            input=json.dumps(manifest), capture_output=True, text=True,
        )
        # stdout kubectl apply — «secret/x created», значений в нём нет.
        print(f"  {res.stdout.strip() or res.stderr.strip()}")
        rc = rc or res.returncode
    if not do_apply and rc == 0:
        print("\nЭто план. Создать: python3 scripts/split_role_secrets.py apply")
    return rc


def _settings_overlap(args: argparse.Namespace) -> Dict[str, Tuple[List[str], List[str]]]:
    settings = parse_configmap_data(SETTINGS_FILE.read_text(encoding="utf-8"))
    names = [args.source] + [_role_spec(r)[0] for r in ROLES]
    out: Dict[str, Tuple[List[str], List[str]]] = {}
    for name in names:
        data = _secret_data(args.namespace, name)
        if data is not None:
            out[name] = compare_settings(settings, data)
    return out


def cmd_verify(args: argparse.Namespace) -> int:
    rc = 0
    for name, (same, differ) in _settings_overlap(args).items():
        if not same and not differ:
            print(f"{name}: пересечений с ConfigMap нет")
            continue
        if same:
            print(f"{name}: дублируют ConfigMap (значения совпадают): {', '.join(same)}")
        if differ:
            rc = 1
            print(
                f"{name}: РАСХОДЯТСЯ с ConfigMap: {', '.join(differ)}\n"
                "  Пока ключ в Secret, действует значение из Secret. Посмотреть его:\n"
                f"    kubectl -n {args.namespace} get secret {name} "
                "-o go-template='{{index .data \"КЛЮЧ\" | base64decode}}'\n"
                "  и привести k8s/base/settings.yaml к живому значению (или решить иначе осознанно)."
            )
    return rc


def cmd_cleanup(args: argparse.Namespace) -> int:
    overlap = _settings_overlap(args).get(args.source)
    if overlap is None:
        print(f"ERROR: Secret {args.source} не найден в ns {args.namespace}", file=sys.stderr)
        return 1
    same, differ = overlap
    if differ:
        print(
            f"ERROR: расходятся с ConfigMap: {', '.join(differ)} — сначала `verify`.",
            file=sys.stderr,
        )
        return 1
    source = _secret_data(args.namespace, args.source) or {}
    keys = same + [k for k in EXTRA_CLEANUP_KEYS if k in source]
    if not keys:
        print("Удалять нечего: флагов в Secret уже нет.")
        return 0
    settings = parse_configmap_data(SETTINGS_FILE.read_text(encoding="utf-8"))
    print(f"# Ключи к удалению из {args.source}: {', '.join(keys)}")
    print("# Поведение не меняется: значения совпадают с ConfigMap, ENV задан литералом.")
    print(
        f"kubectl -n {args.namespace} patch secret {args.source} --type=json "
        f"-p '{cleanup_patch(keys)}'"
    )
    # Откат не требует копии Secret на диске: удаляются только несекретные
    # флаги, и их значения равны значениям ConfigMap (это только что проверено).
    restore = {k: settings[k] for k in same}
    print("# Откат (вернуть флаги в Secret; ENV не восстанавливается — он не нужен):")
    print(
        f"kubectl -n {args.namespace} patch secret {args.source} "
        f"-p '{json.dumps({'stringData': restore})}'"
    )
    return 0


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("command", nargs="?", default="plan",
                    choices=["plan", "apply", "verify", "cleanup"])
    ap.add_argument("roles", nargs="*", default=[],
                    help=f"роли для plan/apply: {', '.join(ROLES)} (по умолчанию все)")
    ap.add_argument("--namespace", default="sre-ai")
    ap.add_argument("--source", default="sre-ai-secrets")
    args = ap.parse_args(argv)
    unknown = [r for r in args.roles if r not in ROLES]
    if unknown:
        ap.error(f"неизвестная роль: {', '.join(unknown)} (есть: {', '.join(ROLES)})")

    if args.command == "plan":
        return cmd_plan_apply(args, do_apply=False)
    if args.command == "apply":
        return cmd_plan_apply(args, do_apply=True)
    if args.command == "verify":
        return cmd_verify(args)
    return cmd_cleanup(args)


if __name__ == "__main__":
    sys.exit(main())
