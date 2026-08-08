# tests/kg/conftest.py — наследует все фикстуры из родительского
# tests/conftest.py (sys.path setup, llm-pipeline enable). Нужен только
# чтобы pytest нашёл root-conftest при `pytest tests/kg/`.
import pytest

from app.knowledge_graph.edge_decay_guard import reset_source_reports


@pytest.fixture(autouse=True)
def _clean_source_reports():
    """Реестр stats-отчётов синков (`edge_decay_guard`) живёт на уровне
    модуля и переживает тест. Любой тест, который зовёт реальный синк
    (`sync_all_ingresses`, `sync_topology`, ...), оставляет в нём запись и
    подменяет здоровье источника соседям. Чистим до и после каждого теста.
    """
    reset_source_reports()
    yield
    reset_source_reports()
