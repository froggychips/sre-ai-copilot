"""Синтаксические проверки наших shell-скриптов.

Падают при опечатке/удалении переменной без bash-выполнения скрипта целиком —
полезно как ранний guard для scripts/*.sh в CI.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent


@pytest.mark.parametrize("script_name", ["run_e2e_local.sh", "ci-local.sh"])
def test_bash_script_parses(script_name):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available")
    path = REPO_ROOT / "scripts" / script_name
    if not path.exists():
        pytest.skip(f"{script_name} not present")
    result = subprocess.run(
        [bash, "-n", str(path)], capture_output=True, text=True
    )
    assert result.returncode == 0, f"bash -n {script_name} failed: {result.stderr}"


def test_deploy_script_parses():
    """deploy.sh — единственный путь выката, синтаксис проверяем так же."""
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available")
    path = REPO_ROOT / "deploy.sh"
    result = subprocess.run([bash, "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, f"bash -n deploy.sh failed: {result.stderr}"


def test_deploy_script_fails_on_failed_rollout():
    """Неуспешный rollout обязан давать не-OK и ненулевой exit code.

    Раньше строка была `kubectl rollout status ... || true`, и финальное
    «Deploy OK» печаталось при ImagePullBackOff, CrashLoopBackOff и зависшем
    на readiness поде — скрипт возвращал 0 и врал о результате выката. Тот же
    класс дефекта, что «зелёный билд при мёртвой статике»: единственный сигнал
    об успехе не связан с успехом.
    """
    text = (REPO_ROOT / "deploy.sh").read_text(encoding="utf-8")

    # Комментарии отбрасываем: в них и `|| true`, и «Deploy OK» упомянуты как
    # раз в объяснении, почему так делать нельзя.
    code_lines = [
        ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
    ]

    rollout_lines = [ln for ln in code_lines if "rollout status" in ln]
    assert rollout_lines, "в deploy.sh пропал `kubectl rollout status`"
    for line in rollout_lines:
        assert "|| true" not in line, (
            f"результат rollout снова проглатывается: {line.strip()}"
        )

    # Финальный «Deploy OK» — только ПОСЛЕ guard-а, который делает exit 1.
    failed_at = next(
        (i for i, ln in enumerate(code_lines) if "Deploy FAILED" in ln), None
    )
    ok_at = next((i for i, ln in enumerate(code_lines) if "Deploy OK" in ln), None)
    assert failed_at is not None, "нет ветки с явным non-OK для упавшего выката"
    assert ok_at is not None, "пропало финальное «Deploy OK»"
    assert failed_at < ok_at, "«Deploy OK» печатается раньше проверки rollout"
    assert any("exit 1" in ln for ln in code_lines[failed_at:ok_at]), (
        "ветка неуспешного rollout не завершается ненулевым exit code"
    )
