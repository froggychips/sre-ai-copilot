"""«Reserved for» на витрине сквадов берётся из squad-mapping.yaml (wo-k8s).

До 04.11.2026 колонка читалась из ручной копии в env RESERVED_MAP, и она
отстала от git на месяц: squad-40/41 показывали резерв за заблокированной
учёткой, хотя в маппинге их уже передали другому разработчику.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest
import requests

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "squad_dashboard.py"

MAPPING = """
squads:
  squad-40: dev-28
  squad-41: dev-28
  squad-68: dev-36
owners:
  squad-40: alice
  squad-41: alice
  squad-50: bob
  squad-77: carol
retired:
  squad-50: prod-k8
"""


@pytest.fixture
def dash(monkeypatch):
    os.environ.setdefault("TC_URL", "https://teamcity.invalid")
    os.environ.setdefault("TC_TOKEN", "x")
    spec = importlib.util.spec_from_file_location("squad_dashboard_reserved_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_owner_and_node_from_mapping(dash):
    import yaml
    got = dash.reserved_from_mapping(yaml.safe_load(MAPPING))
    assert got["squad-40"] == "alice · dev-28"
    assert got["squad-41"] == "alice · dev-28"
    assert got["squad-77"] == "carol"          # нода не закреплена — только логин
    assert "squad-50" not in got               # retired не показываем
    assert "squad-68" not in got               # нода есть, владельца нет — не резерв


class _Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def test_reserved_map_reads_gitlab(dash, monkeypatch):
    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen.update(url=url, headers=headers)
        return _Resp(MAPPING)

    monkeypatch.setenv("SQUAD_MAPPING_URL", "https://gitlab.invalid/raw")
    monkeypatch.setenv("SQUAD_MAPPING_TOKEN", "t0k")
    monkeypatch.setattr(dash.requests, "get", fake_get)
    assert dash.reserved_map()["squad-40"] == "alice · dev-28"
    assert seen["headers"] == {"PRIVATE-TOKEN": "t0k"}


def test_gitlab_down_leaves_column_empty(dash, monkeypatch):
    monkeypatch.setenv("SQUAD_MAPPING_URL", "https://gitlab.invalid/raw")
    monkeypatch.setattr(dash.requests, "get", lambda *a, **k: _Resp("", 404))
    assert dash.reserved_map() == {}


def test_not_configured_leaves_column_empty(dash, monkeypatch):
    monkeypatch.delenv("SQUAD_MAPPING_URL", raising=False)
    assert dash.reserved_map() == {}
