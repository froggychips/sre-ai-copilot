"""Тесты `scripts/check_no_pii.py` — стража персональных данных.

Repo публичный, поэтому страж обязан ловить ровно то, на чём обожглись 15.09.2026:
карта «squad-N -> логин · нода» в коде, ФИО сотрудников в фикстурах, кадровые
формулировки в сообщениях коммитов, корпоративная почта.

Покрытие:
  - каждое из четырёх правил срабатывает на характерном примере;
  - плейсхолдеры (NATO), технические учётки и боты не считаются людьми;
  - `pii-allow` в строке снимает находку;
  - слова, которые в SRE-коде значат другое (`alert fired`, «транзиент, ушедший
    на 3-й попытке»), не дают ложных срабатываний — на них правило уже спотыкалось;
  - сам репозиторий проходит проверку (регрессия на будущее).
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_no_pii.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_no_pii", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


checker = _load()


def _scan(tmp_path: Path, text: str, name: str = "sample.py"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return checker.scan_file(path)


# ── правила срабатывают ───────────────────────────────────────────────────────

def test_login_node_pair_detected(tmp_path: Path):
    """Связка человека с нодой — то, что лежало в squad_dashboard.py."""
    found = _scan(tmp_path, 'RESERVED = {"squad-58": "jsmith · dev-37"}')  # pii-allow: фикстура
    assert [rule for _, rule, _ in found] == ["логин↔нода"]


def test_corporate_email_detected(tmp_path: Path):
    found = _scan(tmp_path, 'CONTACT = "ivan.petrov@lastoasisgame.com"')  # pii-allow: фикстура
    assert [rule for _, rule, _ in found] == ["корпоративный e-mail"]


def test_cyrillic_full_name_detected(tmp_path: Path):
    found = _scan(tmp_path, 'OWNER = "Василий Кузнецов"')  # pii-allow: фикстура
    assert [rule for _, rule, _ in found] == ["кириллическое ФИО"]


def test_hr_wording_detected(tmp_path: Path):
    found = _scan(tmp_path, "# резерв переходит от ушедшего сотрудника к новому")  # pii-allow: фикстура
    assert [rule for _, rule, _ in found] == ["кадровая формулировка"]


# ── ложных срабатываний быть не должно ────────────────────────────────────────

@pytest.mark.parametrize("login", ["alpha", "foxtrot", "zulu", "teamcity", "ai-agent", "alice"])
def test_placeholder_and_service_logins_allowed(tmp_path: Path, login: str):
    """NATO-плейсхолдеры, боты и тестовые имена — не люди."""
    assert _scan(tmp_path, f'RESERVED = {{"squad-1": "{login} · dev-28"}}') == []


def test_alert_fired_is_not_hr_wording(tmp_path: Path):
    """В SRE-коде `fired` — это сработавший алерт; на этом правило спотыкалось."""
    assert _scan(tmp_path, "if alert.fired and rule_fired_at:  # alert fired") == []


def test_transient_gone_is_not_hr_wording(tmp_path: Path):
    """«транзиент, ушедший на 3-й попытке» — техническая фраза."""  # pii-allow: фикстура
    assert _scan(tmp_path, "# транзиент, ушедший на 3-й попытке, даёт вердикт") == []


def test_domain_phrase_is_not_a_person(tmp_path: Path):
    """Разрешённые вымышленные имена из фикстур не считаются находкой."""
    assert _scan(tmp_path, 'USER = "Иван Иванов"  # фикстура') == []


def test_pii_allow_comment_suppresses(tmp_path: Path):
    found = _scan(tmp_path, 'OWNER = "Василий Кузнецов"  # pii-allow: пример')  # pii-allow: фикстура
    assert found == []


# ── регрессия ────────────────────────────────────────────────────────────────

def test_repository_itself_is_clean():
    """Основная защита: сам репозиторий не должен содержать персональных данных."""
    res = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert res.returncode == 0, f"PII-страж нашёл персональные данные:\n{res.stdout}"
