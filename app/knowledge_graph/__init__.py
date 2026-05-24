"""Knowledge graph: service / deployment / alert / edges.

Минимальный SQL-граф поверх той же SQLAlchemy-сессии, что и инциденты.
Не Neo4j, не TigerGraph — мы оптимизируем под «несколько тысяч сервисов
и пара тысяч edges». Для этого реляционных таблиц с правильными
индексами хватает за глаза, а отдельная инфраструктура не нужна.

Узлы (`Service`, `Deployment`, `AlertEvent`) и рёбра (`ServiceEdge`)
заполняются populator-ами из:
  * k8s API (services, replicasets → ownership)
  * git deploy logs / TeamCity API (deployments)
  * Alertmanager webhook history (incidents)

Сейчас populator-ы — stub-ы; реальный backfill — отдельный шаг
после интеграции в pipeline (см. C/D).
"""

from app.knowledge_graph.contract import (
    EDGE_KINDS,
    KG_SCHEMA_VERSION,
    QUALITY_THRESHOLDS,
    is_orphan,
    is_synthetic,
)
from app.knowledge_graph.queries import (
    incidents_on,
    nearby_alerts,
    recent_deploys_for,
    upstream_of,
)
from app.knowledge_graph.schema import (
    AlertEvent,
    Deployment,
    Service,
    ServiceEdge,
)

__all__ = [
    "Service",
    "Deployment",
    "AlertEvent",
    "ServiceEdge",
    "recent_deploys_for",
    "upstream_of",
    "incidents_on",
    "nearby_alerts",
    "KG_SCHEMA_VERSION",
    "EDGE_KINDS",
    "QUALITY_THRESHOLDS",
    "is_orphan",
    "is_synthetic",
]
