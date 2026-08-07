"""kg_services.node_kind: развести k8s Service и workload

Revision ID: 20260807_0200
Revises: 20260610_0100
Create Date: 2026-08-07 15:30:00.000000

kg_services хранила всё одним типом, и k8s Service схлопывался с k8s
Deployment по (namespace, name) — `auth-service` был одним узлом. Поэтому
ребро serves_traffic (Service → backing workload) не могло существовать: оно
всегда получалось self-loop. Замер на живом графе 07.08.2026: каждый тик
синка отбрасывалось 2092 self-loop и 2231 no_match, а рёбер serves_traffic
в графе оставалось ровно 3 (и это выглядело как «мёртвое ребро» в KG
quality). Отсюда же раздутый orphan: 4523 из 5799 real-сервисов формально
без HTTP-связей.

Все существующие строки получают node_kind='service' — смысл старых данных
не меняется. Уникальный ключ расширяется до (namespace, name, node_kind),
чтобы Service и workload с одинаковым именем были разными узлами.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260807_0200"
down_revision = "20260610_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "kg_services",
        sa.Column(
            "node_kind", sa.String(), nullable=False, server_default="service",
        ),
    )
    op.create_index("ix_kg_services_node_kind", "kg_services", ["node_kind"])
    # Старый ключ (namespace, name) не даст завести workload с именем
    # существующего Service — меняем на трёхколоночный.
    op.drop_constraint("uq_kg_service_ns_name", "kg_services", type_="unique")
    op.create_unique_constraint(
        "uq_kg_service_ns_name_kind", "kg_services", ["namespace", "name", "node_kind"],
    )


def downgrade() -> None:
    # Откат возможен только если non-service узлов не осталось: иначе
    # (namespace, name) перестанет быть уникальным и constraint не создастся.
    # Удаляем их явно — это производные данные, синк восстановит.
    #
    # Сначала ссылки: workload-узел почти всегда уже висит в
    # kg_service_edges как dst ребра serves_traffic, и голый DELETE падает на
    # kg_service_edges_dst_id_fkey (проверено на реальном откате).
    # Обнуляемые FK зануляем, NOT NULL — удаляем строки; всё это
    # производные данные синков, не пользовательский ввод.
    op.execute("""
        DELETE FROM kg_service_edges
        WHERE src_id IN (SELECT id FROM kg_services WHERE node_kind <> 'service')
           OR dst_id IN (SELECT id FROM kg_services WHERE node_kind <> 'service')
    """)
    # Запросы выписаны литералами, без интерполяции имени таблицы в SQL:
    # цикл по списку таблиц с f-string — это B608 у bandit, а подавлять
    # предупреждение в миграции, которая удаляет данные, не хочется.
    #
    # service_id NOT NULL → удаляем строку:
    op.execute("DELETE FROM kg_anomaly_observations WHERE service_id IN "
               "(SELECT id FROM kg_services WHERE node_kind <> 'service')")
    op.execute("DELETE FROM kg_deployments WHERE service_id IN "
               "(SELECT id FROM kg_services WHERE node_kind <> 'service')")
    op.execute("DELETE FROM kg_service_health WHERE service_id IN "
               "(SELECT id FROM kg_services WHERE node_kind <> 'service')")
    op.execute("DELETE FROM kg_signal_aggregates WHERE service_id IN "
               "(SELECT id FROM kg_services WHERE node_kind <> 'service')")
    # service_id nullable → достаточно обнулить:
    op.execute("UPDATE kg_alerts SET service_id = NULL WHERE service_id IN "
               "(SELECT id FROM kg_services WHERE node_kind <> 'service')")
    op.execute("UPDATE kg_ingress_observations SET service_id = NULL "
               "WHERE service_id IN "
               "(SELECT id FROM kg_services WHERE node_kind <> 'service')")
    op.execute("UPDATE kg_log_observations SET service_id = NULL "
               "WHERE service_id IN "
               "(SELECT id FROM kg_services WHERE node_kind <> 'service')")
    op.execute("UPDATE kg_pod_events SET service_id = NULL WHERE service_id IN "
               "(SELECT id FROM kg_services WHERE node_kind <> 'service')")
    op.execute("DELETE FROM kg_services WHERE node_kind <> 'service'")
    op.drop_constraint("uq_kg_service_ns_name_kind", "kg_services", type_="unique")
    op.create_unique_constraint(
        "uq_kg_service_ns_name", "kg_services", ["namespace", "name"],
    )
    op.drop_index("ix_kg_services_node_kind", table_name="kg_services")
    op.drop_column("kg_services", "node_kind")
