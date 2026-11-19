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


# ── публичные IPv4 (18.11.2026) ───────────────────────────────────────────────
#
# В манифестах лежали адрес внешней базы с NodePort и адреса ingress-нод.
# Адреса в фикстурах ниже — не наши: это давний адрес example.com.

def test_public_ipv4_detected(tmp_path: Path):
    found = _scan(tmp_path, "cidr: 93.184.216.34/32", name="np.yaml")  # pii-allow: фикстура
    assert [(rule, frag) for _, rule, frag in found] == [("публичный IPv4", "93.184.216.34")]  # pii-allow: фикстура


def test_public_ipv4_in_comment_detected(tmp_path: Path):
    """Адрес в комментарии — та же утечка: именно так лежали адреса нод."""
    found = _scan(tmp_path, "# проверено из пода: 93.184.216.34 — TimeoutError")  # pii-allow: фикстура
    assert [rule for _, rule, _ in found] == ["публичный IPv4"]


@pytest.mark.parametrize("text", [
    "cidr: 10.0.0.0/8",                 # RFC1918 — except у NetworkPolicy
    "cidr: 172.16.0.0/12",
    "cidr: 192.168.0.0/16",
    "host = '10.43.0.1'",               # service CIDR кластера
    "cidr: 0.0.0.0/0",
    "bind 127.0.0.1",
    "METRICS_BIND_ADDR = '0.0.0.0'",
    "169.254.169.254",                  # link-local (metadata)
    "STATICS_DB_CIDR: 203.0.113.10/32",  # RFC 5737, диапазон для документации
    "dns: 8.8.8.8",                     # общеизвестный резолвер
    "click-plugins==1.1.1.2",           # версия пакета
    "Chrome/999.0.0.1",                 # не адрес: октет > 255
    "version 1.0.35 and 1.0.0-rc.19",   # три компонента — не адрес
])
def test_non_public_ipv4_allowed(tmp_path: Path, text: str):
    assert _scan(tmp_path, text) == []


def test_ip_legacy_files_are_exact_and_few():
    """Список исключений не должен расти: для единичного случая есть pii-allow."""
    assert checker.IP_LEGACY_FILES == {"CHANGELOG.md", "app/services/node_resolver.py"}


def test_k8s_manifests_carry_no_public_ipv4():
    """Манифесты — главное место, где адреса уже лежали; исключений тут нет."""
    for path in sorted((ROOT / "k8s").rglob("*")):
        if path.is_file():
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                assert checker.find_public_ipv4(line) == [], f"{path}:{lineno}"
