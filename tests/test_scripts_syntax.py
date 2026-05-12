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
