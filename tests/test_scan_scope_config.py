"""Настройки, влияющие на работу задачи, должны стоять там, где она бежит.

Прецедент 22.08.2026. `KG_SCAN_NAMESPACES` был задан у `copilot-beat`, а
`kg_topology_sync` выполняет `copilot-worker` — переменную он не видел, и
синк всё это время шёл через auto-discovery. Само по себе это работало
хорошо (198 namespace за 219 секунд, в графе обновлялось ровно 198), но
список в манифесте выглядел как действующая настройка: по нему в CHANGELOG
попало ограничение «сканируется 16 из 98 прикладных», которого не
существовало.

Конфигурация, которую никто не читает, хуже отсутствующей — она выглядит
как факт. Этот тест ловит именно такой случай: настройка есть, а компонент,
который её читает, её не получает.
"""
import pathlib

import pytest
import yaml

#: Переменная → задачи, которые её читают. Задачи бегут на worker.
_WORKER_ONLY_SETTINGS = {
    "KG_SCAN_NAMESPACES": "kg_topology_sync",
    "KG_DB_EDGE_REHOME_ENABLED": "kg_db_edge_rehome",
    "KG_REINCARNATION_PURGE_ENABLED": "kg_namespace_lifecycle",
}

_MANIFESTS = ("k8s/worker.yaml", "k8s/base/deployment.yaml")


def _deployments():
    root = pathlib.Path(__file__).parent.parent
    out = {}
    for rel in _MANIFESTS:
        for doc in yaml.safe_load_all((root / rel).read_text()):
            if doc and doc.get("kind") == "Deployment":
                c = doc["spec"]["template"]["spec"]["containers"][0]
                out[doc["metadata"]["name"]] = {
                    e["name"] for e in c.get("env", [])
                }
    return out


@pytest.mark.parametrize("var,task", sorted(_WORKER_ONLY_SETTINGS.items()))
def test_task_settings_are_not_set_on_beat_alone(var, task):
    """Настройка задачи на одном beat не действует: задачу выполняет worker."""
    envs = _deployments()
    on_beat = var in envs.get("copilot-beat", set())
    on_worker = var in envs.get("copilot-worker", set())
    assert not (on_beat and not on_worker), (
        f"{var} задана только у copilot-beat, а её читает {task} на "
        f"copilot-worker — настройка не действует, но выглядит действующей"
    )


def test_beat_and_worker_share_the_same_secret_source():
    """Оба компонента читают один Secret: расхождение здесь тоже невидимо."""
    root = pathlib.Path(__file__).parent.parent
    refs = {}
    for doc in yaml.safe_load_all((root / "k8s/worker.yaml").read_text()):
        if doc and doc.get("kind") == "Deployment":
            c = doc["spec"]["template"]["spec"]["containers"][0]
            refs[doc["metadata"]["name"]] = {
                (f.get("secretRef") or {}).get("name")
                for f in c.get("envFrom", [])
            }
    assert refs["copilot-worker"] == refs["copilot-beat"], refs
