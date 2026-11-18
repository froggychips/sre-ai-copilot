"""`scripts/split_role_secrets.py` — раскладка общего Secret по ролям.

Скрипт выполняет человек на боевом кластере, поэтому проверяем то, что нельзя
проверять на бою: что в Secret роли попадают только ключи из шаблона, что
значения не перекодируются и что сверка с ConfigMap не путает «совпало» с
«расходится». kubectl здесь не вызывается.
"""
from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "split_role_secrets.py"


def _load():
    spec = importlib.util.spec_from_file_location("split_role_secrets", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


SOURCE = {
    "DATABASE_URL": _b64("postgresql://u:p@postgres/db"),
    "REDIS_URL": _b64("redis://redis:6379/0"),
    "GITLAB_TOKEN": _b64("glpat-not-for-executor"),
    "CH_PROD_PASSWORD": _b64("not-for-executor"),
    "LLM_BACKEND": _b64("claude_cli"),
    "EXECUTOR_ENABLED": _b64("True\n"),
    "DISCORD_DRY_RUN": _b64("true"),
    "ENV": _b64("development"),
}


@pytest.mark.parametrize("role,name", [
    ("executor", "sre-ai-executor-secrets"), ("beat", "sre-ai-beat-secrets"),
])
def test_examples_parse(role: str, name: str) -> None:
    text = (ROOT / "k8s" / "base" / f"secrets.{role}.yaml.example").read_text(encoding="utf-8")
    parsed_name, required, optional = mod.parse_example(text)
    assert parsed_name == name
    assert set(required) == {"DATABASE_URL", "REDIS_URL"}
    assert "OTLP_EXPORTER_ENDPOINT" in optional and not set(required) & set(optional)


def test_example_without_keys_is_an_error() -> None:
    with pytest.raises(ValueError):
        mod.parse_example("apiVersion: v1\nkind: Secret\nmetadata:\n  name: x\nstringData:\n")


def test_role_secret_gets_only_listed_keys_and_values_untouched() -> None:
    manifest, copied, missing = mod.build_role_secret(
        SOURCE, "sre-ai-executor-secrets", ["DATABASE_URL", "REDIS_URL"], ["OTLP_EXPORTER_ENDPOINT"])
    assert missing == []
    assert copied == ["DATABASE_URL", "REDIS_URL"]
    assert manifest["data"] == {k: SOURCE[k] for k in ("DATABASE_URL", "REDIS_URL")}
    assert manifest["kind"] == "Secret" and manifest["metadata"]["name"] == "sre-ai-executor-secrets"
    # Чужие токены не утекли ни в data, ни куда-либо ещё в манифесте.
    assert "GITLAB_TOKEN" not in json.dumps(manifest)


def test_missing_required_key_is_reported_not_skipped() -> None:
    _, copied, missing = mod.build_role_secret({"REDIS_URL": "eA=="}, "x", ["DATABASE_URL", "REDIS_URL"], [])
    assert copied == ["REDIS_URL"] and missing == ["DATABASE_URL"]


def test_compare_settings_separates_same_from_different() -> None:
    settings = {"LLM_BACKEND": "claude_cli", "EXECUTOR_ENABLED": "true",
                "DISCORD_DRY_RUN": "false", "STATS_DIGEST_ENABLED": "true"}
    same, differ = mod.compare_settings(settings, SOURCE)
    # `True\n` в Secret и "true" в ConfigMap — для Settings одно значение.
    assert same == ["EXECUTOR_ENABLED", "LLM_BACKEND"]
    assert differ == ["DISCORD_DRY_RUN"]
    # Ключа нет в Secret — пересечения нет, это не расхождение.
    assert "STATS_DIGEST_ENABLED" not in same + differ


def test_cleanup_patch_removes_only_given_keys() -> None:
    patch = json.loads(mod.cleanup_patch(["LLM_BACKEND", "ENV"]))
    assert patch == [{"op": "remove", "path": "/data/LLM_BACKEND"},
                     {"op": "remove", "path": "/data/ENV"}]


def test_plan_prints_key_names_but_never_values(monkeypatch, capsys) -> None:
    monkeypatch.setattr(mod, "_secret_data", lambda ns, name: dict(SOURCE))
    assert mod.main(["plan"]) == 0
    out = capsys.readouterr().out
    assert "DATABASE_URL" in out and "sre-ai-executor-secrets" in out
    for secret_value in ("postgresql://u:p@postgres/db", "glpat-not-for-executor",
                         SOURCE["DATABASE_URL"]):
        assert secret_value not in out


def test_verify_fails_on_divergent_flag_and_prints_no_value(monkeypatch, capsys) -> None:
    monkeypatch.setattr(mod, "_secret_data",
                        lambda ns, name: dict(SOURCE) if name == "sre-ai-secrets" else None)
    assert mod.main(["verify"]) == 1
    out = capsys.readouterr().out
    assert "DISCORD_DRY_RUN" in out and "РАСХОДЯТСЯ" in out
    assert SOURCE["DISCORD_DRY_RUN"] not in out


def test_cleanup_refuses_while_values_diverge(monkeypatch, capsys) -> None:
    monkeypatch.setattr(mod, "_secret_data", lambda ns, name: dict(SOURCE))
    assert mod.main(["cleanup"]) == 1
    assert "kubectl" not in capsys.readouterr().out


def test_cleanup_command_lists_duplicates_and_env(monkeypatch, capsys) -> None:
    source = dict(SOURCE, DISCORD_DRY_RUN=_b64("false"))
    monkeypatch.setattr(mod, "_secret_data", lambda ns, name: dict(source))
    assert mod.main(["cleanup"]) == 0
    out = capsys.readouterr().out
    patch_line, restore_line = [line for line in out.splitlines() if line.startswith("kubectl")]
    removed = {op["path"].rsplit("/", 1)[-1] for op in json.loads(patch_line.split("-p '", 1)[1].rstrip("'"))}
    assert removed == {"LLM_BACKEND", "EXECUTOR_ENABLED", "DISCORD_DRY_RUN", "ENV"}
    # Откат — из значений ConfigMap: копия Secret на диске не нужна.
    restore = json.loads(restore_line.split("-p '", 1)[1].rstrip("'"))["stringData"]
    assert restore == {"LLM_BACKEND": "claude_cli", "EXECUTOR_ENABLED": "true", "DISCORD_DRY_RUN": "false"}
    assert "postgresql://" not in out


def test_unknown_role_is_rejected() -> None:
    with pytest.raises(SystemExit):
        mod.main(["apply", "worker"])
