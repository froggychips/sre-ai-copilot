"""Настройки поведения — в ConfigMap, секреты — по ролям (18.11.2026).

До этого дня все пять Deployment-ов подключали один Secret `sre-ai-secrets`
целиком, а флаги поведения («мост включён», «executor включён», «какой
LLM-бэкенд») лежали в нём ключами: из git нельзя было ни прочитать, ни
воспроизвести боевое состояние, а executor — единственный процесс с правом
записи в кластер — получал пароль ClickHouse и токены GitLab/Jira/TeamCity.

Что здесь держится:

* **Порядок envFrom.** ConfigMap первым, Secret роли последним: при совпадении
  ключей побеждает последний источник, поэтому, пока ключ не удалён из Secret,
  применение манифестов поведение не меняет. Перестановка строк не ломает
  рендер и не видна в диффе как ошибка.
* **Предохранители — литералом.** ENV и SAFE_MODE в pod-спеке: литеральный env
  сильнее любого envFrom, а в `sre-ai-secrets` лежит свой ENV.
* **Одна настройка — одно место.** Ключ ConfigMap не может одновременно стоять
  литералом в Deployment-е или числиться в шаблоне Secret-а.
* **Процесс стартует без чужих ключей.** Settings каждой роли поднимается на
  ConfigMap + своих литералах + DATABASE_URL и REDIS_URL — то есть ни один
  токен соседней системы для старта не обязателен.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
K8S = ROOT / "k8s"
SETTINGS_CM = "sre-ai-copilot-settings"

#: Deployment → Secret его роли.
ROLE_SECRET = {
    "sre-ai-api": "sre-ai-secrets",
    "copilot-worker": "sre-ai-secrets",
    "copilot-worker-sync": "sre-ai-secrets",
    "copilot-beat": "sre-ai-beat-secrets",
    "copilot-executor": "sre-ai-executor-secrets",
}

#: Ключи ConfigMap, которые не являются полями Settings, — с причиной.
NON_SETTINGS_KEYS = {
    "PYTHONPATH": "переменная интерпретатора: путь импорта пакета app",
}


def _docs(path: Path) -> list:
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if isinstance(d, dict)]


def _settings_data() -> dict:
    (cm,) = _docs(K8S / "base" / "settings.yaml")
    assert cm["kind"] == "ConfigMap" and cm["metadata"]["name"] == SETTINGS_CM
    return cm["data"]


def _deployments() -> dict:
    out = {}
    for rel in ("base/deployment.yaml", "worker.yaml", "executor.yaml"):
        for d in _docs(K8S / rel):
            if d["kind"] == "Deployment":
                out[d["metadata"]["name"]] = d["spec"]["template"]
    return out


def _container(template: dict) -> dict:
    (container,) = template["spec"]["containers"]
    return container


def _example_keys(name: str) -> set:
    (secret,) = _docs(K8S / "base" / f"secrets.{name}.yaml.example")
    return set(secret["stringData"])


def test_all_five_deployments_are_covered() -> None:
    assert set(_deployments()) == set(ROLE_SECRET)


# ── ConfigMap ────────────────────────────────────────────────────────────────


def test_settings_keys_are_real_settings_fields() -> None:
    """Опечатка в имени ключа не ломает ничего — настройка просто не действует
    (extra="ignore" в Settings), а манифест выглядит так, будто действует."""
    from app.config import Settings

    unknown = set(_settings_data()) - set(Settings.model_fields) - set(NON_SETTINGS_KEYS)
    assert not unknown, f"в k8s/base/settings.yaml нет таких полей Settings: {sorted(unknown)}"


def test_settings_values_are_quoted_strings() -> None:
    """`KEY: true` без кавычек — bool в YAML, и API-сервер отклонит ConfigMap;
    то же с числами. Ошибка всплыла бы только на apply."""
    for key, value in _settings_data().items():
        assert isinstance(value, str), f"{key}: значение должно быть строкой в кавычках"


def test_settings_file_is_parseable_without_yaml() -> None:
    """scripts/split_role_secrets.py разбирает файл построчно (у оператора может
    не быть PyYAML) — его разбор обязан совпадать с настоящим."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "split_role_secrets", ROOT / "scripts" / "split_role_secrets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    text = (K8S / "base" / "settings.yaml").read_text(encoding="utf-8")
    assert mod.parse_configmap_data(text) == _settings_data()


def test_safety_invariants_are_not_in_configmap() -> None:
    """ENV и SAFE_MODE из ConfigMap перекрывались бы ключом Secret (он в envFrom
    последний), а в sre-ai-secrets свой ENV есть."""
    assert not {"ENV", "SAFE_MODE"} & set(_settings_data())


def test_settings_keys_are_not_secrets() -> None:
    """В ConfigMap — только поведение. Имя, похожее на секрет, сюда попасть не должно."""
    suspicious = re.compile(r"(TOKEN|PASSWORD|SECRET|API_KEY|WEBHOOK|DATABASE_URL|REDIS_URL)")
    bad = [k for k in _settings_data() if suspicious.search(k)]
    assert not bad, f"похоже на секрет, место ему в Secret роли: {bad}"


# ── Deployment-ы ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name,secret", sorted(ROLE_SECRET.items()))
def test_env_from_order_configmap_then_role_secret(name: str, secret: str) -> None:
    env_from = _container(_deployments()[name])["envFrom"]
    assert env_from == [
        {"configMapRef": {"name": SETTINGS_CM}},
        {"secretRef": {"name": secret}},
    ], f"{name}: порядок envFrom — контракт (см. шапку k8s/base/settings.yaml)"


@pytest.mark.parametrize("name", sorted(ROLE_SECRET))
def test_safety_invariants_are_literal_env(name: str) -> None:
    env = {e["name"]: e.get("value") for e in _container(_deployments()[name])["env"]}
    assert env.get("ENV") == "production"
    assert env.get("SAFE_MODE") == "true"


@pytest.mark.parametrize("name", sorted(ROLE_SECRET))
def test_configmap_keys_are_not_duplicated_as_literals(name: str) -> None:
    """Литерал сильнее ConfigMap: дубль означал бы, что правка settings.yaml у
    этой роли молча не действует."""
    literal = {e["name"] for e in _container(_deployments()[name])["env"]}
    dup = literal & set(_settings_data())
    assert not dup, f"{name}: {sorted(dup)} — и литерал, и ключ ConfigMap"


@pytest.mark.parametrize("name", sorted(ROLE_SECRET))
def test_pod_template_carries_settings_checksum(name: str) -> None:
    """Без аннотации правка ConfigMap не даёт rollout: под читает envFrom на старте."""
    annotations = _deployments()[name]["metadata"].get("annotations") or {}
    assert annotations.get("checksum/settings") == "SETTINGS_CHECKSUM_PLACEHOLDER"


def test_migrate_job_reads_settings_but_survives_without_them() -> None:
    """alembic/env.py импортирует Settings → Job-у нужен LLM_BACKEND из ConfigMap.
    optional — потому что TeamCity берёт этот файл из master, возможно, раньше,
    чем ConfigMap появится в кластере."""
    (job,) = _docs(K8S / "migrate-job.yaml")
    env_from = _container(job["spec"]["template"])["envFrom"]
    assert env_from[0] == {"configMapRef": {"name": SETTINGS_CM, "optional": True}}
    assert env_from[-1] == {"secretRef": {"name": "sre-ai-secrets"}}


def test_only_executor_and_beat_have_narrow_secrets() -> None:
    """Executor — единственный с write-токеном кластера: общий Secret ему не положен."""
    assert ROLE_SECRET["copilot-executor"] != "sre-ai-secrets"
    assert ROLE_SECRET["copilot-beat"] != "sre-ai-secrets"
    assert _example_keys("executor") >= {"DATABASE_URL", "REDIS_URL"}
    forbidden = re.compile(r"^(CH_PROD_|GITLAB_|JIRA_|TC_|STATICS_|ANTHROPIC_|DISCORD_WEBHOOK|DISCORD_BOT)")
    for role in ("executor", "beat"):
        leaked = [k for k in _example_keys(role) if forbidden.match(k)]
        assert not leaked, f"secrets.{role}.yaml.example: лишние для роли ключи {leaked}"


def test_squad_dashboard_takes_one_key_from_app_secret() -> None:
    """CronJob, который ходит в Confluence, не должен получать весь Secret приложения."""
    (cj,) = _docs(K8S / "squad-dashboard.yaml")
    container = _container(cj["spec"]["jobTemplate"]["spec"]["template"])
    assert all("secretRef" not in f for f in container.get("envFrom") or [])
    refs = {
        e["name"]: e["valueFrom"]["secretKeyRef"]
        for e in container["env"] if "secretKeyRef" in (e.get("valueFrom") or {})
    }
    assert refs["DATABASE_URL"] == {"name": "sre-ai-secrets", "key": "DATABASE_URL"}
    assert {r["name"] for r in refs.values()} == {"sre-ai-secrets", "squad-dashboard-secrets"}


# ── шаблоны Secret-ов ────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["app", "executor", "beat"])
def test_secret_examples_carry_no_values_and_no_flags(role: str) -> None:
    (secret,) = _docs(K8S / "base" / f"secrets.{role}.yaml.example")
    assert secret["kind"] == "Secret"
    assert set(secret["stringData"].values()) == {"REPLACE_ME"}, "в шаблоне — только имена ключей"
    dup = set(secret["stringData"]) & (set(_settings_data()) | {"ENV", "SAFE_MODE"})
    assert not dup, f"{sorted(dup)}: флаг поведения живёт в ConfigMap, а не в Secret"


def test_every_role_secret_has_an_example() -> None:
    names = set()
    for role in ("app", "executor", "beat"):
        (secret,) = _docs(K8S / "base" / f"secrets.{role}.yaml.example")
        names.add(secret["metadata"]["name"])
    assert names == set(ROLE_SECRET.values())


def test_deploy_sh_checks_every_role_secret_and_applies_settings_first() -> None:
    text = (ROOT / "deploy.sh").read_text(encoding="utf-8")
    for secret in set(ROLE_SECRET.values()):
        assert secret in text, f"deploy.sh не проверяет наличие {secret}"
    settings_apply = text.index('apply -f "${SETTINGS_MANIFEST}"')
    assert settings_apply < text.index("apply_with_image k8s/migrate-job.yaml")
    assert settings_apply < text.index("apply_with_image k8s/executor.yaml")


# ── старт процесса на минимальном наборе ─────────────────────────────────────

#: Всё, что роль обязана получить из своего Secret, чтобы Settings поднялся с
#: ENV=production. Остальные поля Settings имеют дефолты.
_MANDATORY_SECRET_ENV = {
    "DATABASE_URL": "postgresql://user:pw@127.0.0.1:1/db",
    "REDIS_URL": "redis://127.0.0.1:1/0",
}


def _role_env(name: str) -> dict:
    env = dict(_settings_data())
    for e in _container(_deployments()[name])["env"]:
        if "value" in e:
            env[e["name"]] = e["value"]
    env.update(_MANDATORY_SECRET_ENV)
    # Каталоги из манифеста (emptyDir, ConfigMap-тома) в тесте не существуют.
    for key in ("PROMETHEUS_MULTIPROC_DIR", "OWNERSHIP_MANIFEST_PATH", "PEOPLE_MANIFEST_PATH"):
        env.pop(key, None)
    env["PATH"] = os.environ.get("PATH", "")
    env["PYTHONPATH"] = str(ROOT)
    return env


def _run(code: str, env: dict) -> subprocess.CompletedProcess:
    # Чистое окружение и чужой cwd: ни переменные раннера, ни .env разработчика
    # не должны подсказать процессу то, чего роль в кластере не получит.
    return subprocess.run(
        [sys.executable, "-c", code], env=env, cwd="/",
        capture_output=True, text=True, timeout=120,
    )


@pytest.mark.parametrize("name", sorted(ROLE_SECRET))
def test_settings_start_in_production_without_foreign_keys(name: str) -> None:
    res = _run(
        "from app.config import Settings; s = Settings(_env_file=None); "
        "assert s.is_production and s.SAFE_MODE; print(s.LLM_BACKEND)",
        _role_env(name),
    )
    assert res.returncode == 0, f"{name}: Settings не стартует\n{res.stderr[-2000:]}"
    assert res.stdout.strip() == "claude_cli"


@pytest.mark.parametrize("name", ["copilot-executor", "copilot-beat"])
def test_celery_app_imports_with_role_secret_only(name: str) -> None:
    """Executor и beat запускаются как `celery -A app.workers.tasks.celery_app`:
    импорт всего приложения обязан пройти на DATABASE_URL + REDIS_URL."""
    res = _run(
        "import app.workers.tasks as t; import app.workers.executor_tasks; "
        "assert 'executor_apply' in t.celery_app.tasks; "
        "assert t.celery_app.conf.beat_schedule",
        _role_env(name),
    )
    assert res.returncode == 0, f"{name}: импорт celery-приложения упал\n{res.stderr[-2000:]}"


def test_settings_refuse_to_start_without_llm_backend_flag() -> None:
    """Обратная проверка: почему LLM_BACKEND нужен в ConfigMap КАЖДОЙ роли."""
    env = _role_env("copilot-executor")
    env.pop("LLM_BACKEND")
    res = _run("from app.config import Settings; Settings(_env_file=None)", env)
    assert res.returncode != 0 and "ANTHROPIC_API_KEY" in res.stderr
