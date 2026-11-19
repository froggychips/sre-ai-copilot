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

Что нашлось 18.11.2026, когда к проверке «описано, но отсутствует» добавились
две обратные — «живёт, но не описано» и «описано иначе»:

  * `ClusterRole/sre-ai-events-reader` и `sre-ai-services-reader` жили только
    в кластере;
  * у StatefulSet-ов стоял hostname-пин, которого не было в git: `apply`
    манифеста его бы снял;
  * CronJob-ы `squad-dashboard` и `ci-deadman` работали на образах 1.0.27 и
    1.0.0-rc.19 при приложении 1.0.35 — выкатка их не обновляла.

Использование:
    python3 scripts/manifest_drift.py                # наличие: описано ↔ живёт
    python3 scripts/manifest_drift.py --diff         # + различия спецификаций
    python3 scripts/manifest_drift.py --json         # для CI/задачи
    python3 scripts/manifest_drift.py --namespace X  # другой namespace

`--diff` рендерит манифесты так же, как деплой (`deploy.sh --render`, образ —
тот, что сейчас у sre-ai-api), и отдаёт их в `kubectl diff`. Это серверный
dry-run: он ничего не меняет, но требует тех же прав, что apply. Secret-ы в
сверке не участвуют ни в одном режиме.

Код возврата: 0 — расхождений нет, 1 — есть. Годится для гейта с доступом к
кластеру; статическая часть (без кластера) — tests/test_manifests_static.py.
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
    "ServiceAccount", "PodDisruptionBudget", "NetworkPolicy",
}

#: Кластерные (не namespaced) — их ищем без -n.
CLUSTER_SCOPED = {"ClusterRole", "ClusterRoleBinding"}

#: Файлы, которые описывают не текущее состояние, а шаблон или пример.
SKIP_FILES = {"secrets.yaml", "migrate-job.yaml"}

#: Виды, по которым ищем «живёт, но не описано». Уже, чем TRACKED_KINDS: Job-ы
#: порождают CronJob-ы, PVC — volumeClaimTemplates, и те, и другие в git не
#: описываются по построению.
LIVE_KINDS = (
    "Deployment", "StatefulSet", "CronJob", "Service", "ConfigMap",
    "Role", "RoleBinding", "ServiceAccount", "PodDisruptionBudget",
    "NetworkPolicy",
)

#: Живёт только в кластере — и это известно. Каждая запись с причиной: список
#: без причин через полгода превращается в «так исторически сложилось».
KNOWN_LIVE_ONLY: Dict[Tuple[str, str], str] = {
    ("ConfigMap", "kube-root-ca.crt"): "создаёт Kubernetes",
    ("ServiceAccount", "default"): "создаёт Kubernetes",
    ("ConfigMap", "sre-ai-copilot-people"):
        "люди и их идентификаторы — в публичный git не идут (k8s/people.example.json)",
    ("ConfigMap", "sre-ai-copilot-ownership"):
        "создаётся из config/ownership.yaml командой из RUNBOOK; живая версия дополнена",
    ("ConfigMap", "sre-ai-copilot-site"):
        "значения площадки для deploy.sh (k8s/site.yaml.example)",
    ("ConfigMap", "squad-dashboard-env"): "адреса внутренних систем; шапка k8s/squad-dashboard.yaml",
    ("ConfigMap", "squad-dashboard-script"): "deploy.sh создаёт из scripts/squad_dashboard.py",
    ("ConfigMap", "ci-deadman-env"): "шапка k8s/ci-deadman.yaml",
    ("ConfigMap", "ci-deadman-script"): "deploy.sh создаёт из scripts/ci_deadman.py",
    ("ClusterRole", "sre-ai-services-reader"):
        "дублирует sre-ai-deployments-reader — удалить из кластера (k8s/base/rbac.yaml)",
    ("ClusterRoleBinding", "sre-ai-services-reader"):
        "дублирует sre-ai-deployments-reader — удалить из кластера (k8s/base/rbac.yaml)",
}

#: Объекты, чьё отличие от рендера — не дрейф: Job миграций пересоздаётся на
#: каждом деплое и живёт час (ttlSecondsAfterFinished).
DIFF_IGNORE = {("Job", "sre-ai-migrate")}

#: Манифесты, которые деплой не применяет, но которые описывают живые объекты.
STANDALONE_MANIFESTS = (
    "postgres.yaml", "redis.yaml", "postgres-backup.yaml",
    "monitoring.yaml", "vmalertmanagerconfig.yaml",
)


#: (kind, name) → namespace, если манифест задаёт его явно. Без этого объект
#: из другого namespace (jaeger в monitoring, github-runner-egress в sre-ai-ci)
#: искался в namespace копилота и вечно числился отсутствующим — ложное
#: расхождение, к которому привыкают.
_DECLARED_NAMESPACE: Dict[Tuple[str, str], str] = {}


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
                declared_ns = (doc.get("metadata") or {}).get("namespace")
                if declared_ns:
                    _DECLARED_NAMESPACE[(kind, name)] = declared_ns
    return out


def _exists(kind: str, name: str, namespace: str) -> bool:
    cmd = ["kubectl", "get", kind.lower(), name, "-o", "name"]
    if kind not in CLUSTER_SCOPED:
        cmd += ["-n", _DECLARED_NAMESPACE.get((kind, name), namespace)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return res.returncode == 0
    except Exception:
        # Недоступный kubectl — не повод объявить всё отсутствующим.
        return True


def _kubectl_json(args: List[str]) -> Any:
    """`kubectl ... -o json` → объект, либо None (kubectl недоступен/ошибка)."""
    try:
        res = subprocess.run(["kubectl", *args, "-o", "json"],
                             capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    if res.returncode != 0 or not res.stdout.strip():
        return None
    try:
        return json.loads(res.stdout)
    except ValueError:
        return None


def _live_objects(namespace: str) -> List[Tuple[str, str]] | None:
    """[(kind, name)] живых объектов LIVE_KINDS в namespace + кластерных ролей
    копилота (по имени `sre-ai*`). None — кластер не ответил: тогда про
    «лишнее» нельзя сказать ничего, и молчать честнее, чем гадать."""
    kinds = ",".join(k.lower() for k in LIVE_KINDS)
    listed = _kubectl_json(["-n", namespace, "get", kinds])
    if listed is None:
        return None
    out = [(i["kind"], i["metadata"]["name"]) for i in listed.get("items", [])]
    cluster = _kubectl_json(["get", "clusterrole,clusterrolebinding"])
    for i in (cluster or {}).get("items", []):
        if i["metadata"]["name"].startswith("sre-ai"):
            out.append((i["kind"], i["metadata"]["name"]))
    return out


def find_extra(live: List[Tuple[str, str]],
               described: List[Tuple[str, str, str]]) -> List[Dict[str, str]]:
    """Живёт, в git не описано и в KNOWN_LIVE_ONLY не объяснено."""
    known = {(k, n) for k, n, _ in described}
    return [
        {"kind": k, "name": n}
        for k, n in sorted(set(live))
        if (k, n) not in known and (k, n) not in KNOWN_LIVE_ONLY
    ]


def parse_kubectl_diff(text: str) -> Dict[str, List[str]]:
    """Вывод `kubectl diff` → {"Kind/name": [изменённые строки]}.

    Заголовок каждого объекта — `diff -u -N <dir>/LIVE-…/<group>.<ver>.<Kind>.<ns>.<name>`;
    у core-группы группа пустая (`v1.ConfigMap.ns.name`). `generation`
    меняется при любом dry-run и расхождением не считается: объект, у
    которого кроме него ничего не отличается, в результат не попадает.
    """
    out: Dict[str, List[str]] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("diff "):
            ident = line.split()[-1].rsplit("/", 1)[-1]
            parts = ident.split(".")
            kind_idx = next((i for i, p in enumerate(parts) if p[:1].isupper()), None)
            if kind_idx is None or len(parts) < kind_idx + 3:
                current = ident
            else:
                current = f"{parts[kind_idx]}/{'.'.join(parts[kind_idx + 2:])}"
            out[current] = []
            continue
        if current is None or line.startswith(("+++", "---", "@@")):
            continue
        if line[:1] in "+-" and "generation:" not in line:
            out[current].append(line)
    return {
        k: v for k, v in out.items()
        if v and tuple(k.split("/", 1)) not in DIFF_IGNORE
    }


def _live_image(namespace: str) -> Tuple[str, str] | None:
    """(repo, tag) образа sre-ai-api — чтобы рендер сравнивался с тем, что
    выкачено, и сам образ расхождением не считался."""
    obj = _kubectl_json(["-n", namespace, "get", "deployment", "sre-ai-api"])
    try:
        image = obj["spec"]["template"]["spec"]["containers"][0]["image"]
    except (TypeError, KeyError, IndexError):
        return None
    repo, _, tag = image.rpartition(":")
    return (repo, tag) if repo and tag else None


def _live_statics_cidr(namespace: str) -> str:
    """CIDR площадки из живой NetworkPolicy — только для сверки: deploy.sh сам
    его из политики не берёт, а без значения рендер невозможен."""
    obj = _kubectl_json(["-n", namespace, "get", "networkpolicy", "copilot-network-policy"])
    for rule in ((obj or {}).get("spec") or {}).get("egress") or []:
        if any(p.get("port") == 31700 for p in rule.get("ports") or []):
            for peer in rule.get("to") or []:
                cidr = (peer.get("ipBlock") or {}).get("cidr")
                if cidr:
                    return cidr
    return "none"


def spec_diff(namespace: str) -> Dict[str, List[str]] | None:
    """Различия спецификаций «рендер из git ↔ кластер». None — сверить не
    удалось (нет kubectl, нет прав на dry-run, не определился образ)."""
    import os

    image = _live_image(namespace)
    if image is None:
        return None
    env = dict(os.environ, IMAGE_REPO=image[0], IMAGE_TAG=image[1])
    env.setdefault("STATICS_DB_CIDR", _live_statics_cidr(namespace))
    try:
        rendered = subprocess.run(
            ["bash", str(REPO / "deploy.sh"), "--render", namespace],
            capture_output=True, text=True, timeout=60, env=env, cwd=REPO,
        )
        if rendered.returncode != 0:
            return None
        chunks = [rendered.stdout]
        for name in STANDALONE_MANIFESTS:
            chunks.append("---\n" + (MANIFEST_DIR / name).read_text(encoding="utf-8"))
        res = subprocess.run(
            ["kubectl", "-n", namespace, "diff", "-f", "-"],
            input="\n".join(chunks), capture_output=True, text=True, timeout=180,
        )
    except Exception:
        return None
    # kubectl diff: 0 — различий нет, 1 — есть, >1 — ошибка.
    if res.returncode > 1:
        return None
    return parse_kubectl_diff(res.stdout)


def check(namespace: str, with_diff: bool = False) -> Dict[str, Any]:
    missing: List[Dict[str, str]] = []
    checked = 0
    described = _manifests()
    for kind, name, source in described:
        checked += 1
        if not _exists(kind, name, namespace):
            missing.append({"kind": kind, "name": name, "file": source})
    live = _live_objects(namespace)
    result: Dict[str, Any] = {
        "namespace": namespace, "checked": checked, "missing": missing,
        "extra": find_extra(live, described) if live is not None else [],
        "changed": {},
    }
    if with_diff:
        diff = spec_diff(namespace)
        if diff is None:
            result["diff_error"] = "kubectl diff недоступен (нет доступа или прав на dry-run)"
        else:
            result["changed"] = diff
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--namespace", default="sre-ai")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--diff", action="store_true",
                    help="сверить и спецификации (kubectl diff, серверный dry-run)")
    args = ap.parse_args()

    result = check(args.namespace, True) if args.diff else check(args.namespace)
    extra = result.get("extra") or []
    changed = result.get("changed") or {}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"проверено объектов: {result['checked']}")
        if not result["missing"]:
            print("всё описанное существует в кластере")
        else:
            print(f"ОПИСАНЫ, НО ОТСУТСТВУЮТ ({len(result['missing'])}):")
            for m in result["missing"]:
                print(f"  {m['kind']}/{m['name']}  ← {m['file']}")
            print()
            print("Манифест, который никогда не применялся, — это не")
            print("документация, а обещание. Проверь каждый: он либо нужен")
            print("и должен быть применён, либо устарел и должен быть удалён.")
        if extra:
            print(f"ЖИВУТ, НО НЕ ОПИСАНЫ ({len(extra)}):")
            for e in extra:
                print(f"  {e['kind']}/{e['name']}")
            print("  Описать в k8s/ либо объяснить в KNOWN_LIVE_ONLY — с причиной.")
        if changed:
            print(f"ОПИСАНЫ ИНАЧЕ, ЧЕМ ЖИВУТ ({len(changed)}):")
            for obj, lines in changed.items():
                print(f"  {obj}")
                for line in lines[:12]:
                    print(f"      {line}")
                if len(lines) > 12:
                    print(f"      … ещё {len(lines) - 12} строк")
        if result.get("diff_error"):
            print(f"ВНИМАНИЕ: спецификации не сверены — {result['diff_error']}")
    return 1 if (result["missing"] or extra or changed) else 0


if __name__ == "__main__":
    sys.exit(main())
