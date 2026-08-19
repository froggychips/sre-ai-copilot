#!/usr/bin/env python3
"""Сверка манифестов репозитория с тем, что реально живёт в кластере.

Зачем это, а не Flux/ArgoCD. Полноценный GitOps здесь был бы правильнее, но
он ставит контроллер с правом изменять ресурсы в кластер, который принадлежит
не только копилоту — там живёт боевой игровой бэкенд. Такое решение
принимается командой, а не вписывается по ходу работы. Эта проверка даёт
главное свойство GitOps — расхождение перестаёт быть невидимым — не требуя
ни новых прав, ни нового контроллера: она только читает.

Что нашлось при первом же запуске 19.08.2026:

  * `CronJob/postgres-backup` — описан, никогда не применялся. База 5.5 ГБ
    в одной реплике не бэкапилась вообще;
  * `Deployment/jaeger`, `Service/jaeger` — описаны, отсутствуют;
  * `--concurrency` у воркера: 2 в манифесте, 4 в кластере. Манифест был
    исправлен вместе с арифметикой памяти, но не применён — и воркеры
    продолжали падать по OOM.

Ни один из трёх случаев не виден из кода: чтобы их заметить, нужно спросить
кластер.

Использование:
    python3 scripts/manifest_drift.py                # человекочитаемо
    python3 scripts/manifest_drift.py --json         # для CI/задачи
    python3 scripts/manifest_drift.py --namespace X  # другой namespace

Код возврата: 0 — расхождений нет, 1 — есть. Годится для CI-гейта.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from typing import Any, Dict, List, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("нужен pyyaml: pip install pyyaml")

REPO = pathlib.Path(__file__).resolve().parent.parent
MANIFEST_DIR = REPO / "k8s"

#: Ресурсы, отсутствие которых значимо. Namespace/Secret не проверяем: первые
#: создаются вне репозитория, вторые намеренно не хранятся в git.
TRACKED_KINDS = {
    "Deployment", "StatefulSet", "CronJob", "Job",
    "Service", "PersistentVolumeClaim", "ConfigMap",
    "ClusterRole", "ClusterRoleBinding", "Role", "RoleBinding",
    "ServiceAccount",
}

#: Кластерные (не namespaced) — их ищем без -n.
CLUSTER_SCOPED = {"ClusterRole", "ClusterRoleBinding"}

#: Файлы, которые описывают не текущее состояние, а шаблон или пример.
SKIP_FILES = {"secrets.yaml", "migrate-job.yaml"}


def _manifests() -> List[Tuple[str, str, str]]:
    """[(kind, name, файл)] из k8s/*.yaml."""
    out: List[Tuple[str, str, str]] = []
    for path in sorted(MANIFEST_DIR.rglob("*.yaml")):
        if path.name in SKIP_FILES:
            continue
        try:
            docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except Exception:
            # Шаблоны с placeholder-ами (IMAGE_PLACEHOLDER) — не наша забота.
            continue
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            kind = doc.get("kind")
            name = (doc.get("metadata") or {}).get("name")
            if kind in TRACKED_KINDS and name:
                out.append((kind, name, path.name))
    return out


def _exists(kind: str, name: str, namespace: str) -> bool:
    cmd = ["kubectl", "get", kind.lower(), name, "-o", "name"]
    if kind not in CLUSTER_SCOPED:
        cmd += ["-n", namespace]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return res.returncode == 0
    except Exception:
        # Недоступный kubectl — не повод объявить всё отсутствующим.
        return True


def check(namespace: str) -> Dict[str, Any]:
    missing: List[Dict[str, str]] = []
    checked = 0
    for kind, name, source in _manifests():
        checked += 1
        if not _exists(kind, name, namespace):
            missing.append({"kind": kind, "name": name, "file": source})
    return {"namespace": namespace, "checked": checked, "missing": missing}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--namespace", default="sre-ai")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    result = check(args.namespace)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"проверено объектов: {result['checked']}")
        if not result["missing"]:
            print("расхождений нет: всё описанное существует в кластере")
        else:
            print(f"ОПИСАНЫ, НО ОТСУТСТВУЮТ ({len(result['missing'])}):")
            for m in result["missing"]:
                print(f"  {m['kind']}/{m['name']}  ← {m['file']}")
            print()
            print("Манифест, который никогда не применялся, — это не")
            print("документация, а обещание. Проверь каждый: он либо нужен")
            print("и должен быть применён, либо устарел и должен быть удалён.")
    return 1 if result["missing"] else 0


if __name__ == "__main__":
    sys.exit(main())
