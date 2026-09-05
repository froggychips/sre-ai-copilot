"""kg_*: снять индексы, которые дублируют уже существующие

Revision ID: 20260905_0100
Revises: 20260819_0200
Create Date: 2026-09-05 01:00:00.000000

Замер на проде 05.09.2026: около 1.7 ГБ индексов, ни один из которых не
отвечает на вопрос, на который уже не отвечает соседний. Половина базы
(6 ГБ) — это индексы, и большая их часть — копии.

Три источника, все в объявлениях моделей:

**1. `id = Column(Integer, primary_key=True, index=True)`** — 15 таблиц из
16. PRIMARY KEY уже создаёт уникальный индекс по той же колонке, `index=True`
добавляет к нему второй, обычный. На `kg_service_health` это 423 МБ:

    pk_kg_service_health   id   UNIQUE   423 МБ   idx_scan          0
    ix_kg_service_health_id id           423 МБ   idx_scan 13 062 195

Планировщик выбирает один из двух и о существовании второго не жалеет.

**2. `Index(...)` поверх `UniqueConstraint(...)` с тем же составом колонок:**

    uq_kg_service_health_service_ts   (service_id, ts) UNIQUE  928 МБ
    ix_kg_service_health_service_ts   (service_id, ts)         928 МБ

Тот же случай в `kg_signal_aggregates`, `kg_cluster_observations`,
`kg_remediation_decisions`.

**3. Одноколоночный индекс, являющийся префиксом составного** — b-tree
по (a, b) обслуживает запросы по `a` не хуже, чем отдельный индекс по `a`.

Каждый лишний индекс — это не только место: он обновляется на каждой
вставке. В `kg_service_health` приезжает 377 тысяч строк в сутки, и три
копии из шести делали ровно ту работу, которую уже делали оригиналы.

Что НЕ трогаем: `ix_kg_ingress_observations_service_id` (27,9 млн сканов,
составного с `service_id` в голове у таблицы нет), `ix_kg_storage_volumes_namespace`
(в составном `namespace` стоит вторым, префиксом не покрывается), уникальные
индексы (держат констрейнты) и два узких горячих индекса, покрытых широкими:
`ix_kg_service_edges_src_id`/`_dst_id` (12,5 млн сканов каждый, 900 КБ против
2,4 МБ у покрывающего) и `ix_kg_volume_edges_src`/`_dst` (825 тысяч сканов
против нуля у `uq_kg_volume_edge_src_dst_kind`). Формально они дубли, но
платить за них дешевле, чем гонять чтения по более широкому b-tree.

DROP INDEX CONCURRENTLY: обычный DROP берёт ACCESS EXCLUSIVE на таблицу, а
`kg_service_health` пишется каждые 10 минут и читается enrichment'ом в
hot-path (POSTMORTEM 2026-08-08 §3.2). CONCURRENTLY не работает внутри
транзакции, поэтому нужен autocommit_block.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260905_0100"
down_revision = "20260819_0200"
branch_labels = None
depends_on = None

#: Дубли PRIMARY KEY по `id` (`primary_key=True, index=True` в модели).
_PK_DUPLICATES = (
    "ix_kg_action_approvals_id",
    "ix_kg_alerts_id",
    "ix_kg_anomaly_observations_id",
    "ix_kg_cluster_observations_id",
    "ix_kg_deployments_id",
    "ix_kg_ingress_observations_id",
    "ix_kg_k8s_jobs_id",
    "ix_kg_log_observations_id",
    "ix_kg_pod_events_id",
    "ix_kg_remediation_decisions_id",
    "ix_kg_service_edges_id",
    "ix_kg_service_health_id",
    "ix_kg_services_id",
    "ix_kg_signal_aggregates_id",
    "ix_kg_storage_volumes_id",
    "ix_kg_volume_edges_id",
)

#: Обычные индексы, повторяющие UNIQUE-констрейнт колонка-в-колонку.
_UNIQUE_DUPLICATES = (
    "ix_kg_service_health_service_ts",       # == uq_kg_service_health_service_ts
    "ix_kg_signal_aggregates_service_window",  # == uq_kg_signal_aggregates_service_window
    "ix_kg_cluster_obs_ts",                  # == uq_kg_cluster_obs_ts
    "ix_kg_remediation_decisions_incident_idem",  # == uq_..._incident_idem
)

#: Одноколоночные индексы, покрытые составным как префикс.
_PREFIX_DUPLICATES = (
    "ix_kg_service_health_service_id",       # ⊂ uq_kg_service_health_service_ts
    "ix_kg_anomaly_obs_service_ts",          # ⊂ uq_kg_anomaly_obs_service_ts_metric
    "ix_kg_anomaly_observations_service_id",  # ⊂ uq_kg_anomaly_obs_service_ts_metric
    "ix_kg_signal_aggregates_service_id",    # ⊂ uq_kg_signal_aggregates_service_window
    "ix_kg_log_observations_service_id",     # ⊂ ix_kg_log_obs_service_ts
    "ix_kg_deployments_service_id",          # ⊂ ix_kg_deploy_service_time
    "ix_kg_pod_events_service_id",           # ⊂ ix_kg_pod_event_service_time
    "ix_kg_alerts_service_id",               # ⊂ ix_kg_alert_service_time
    "ix_kg_k8s_jobs_kind",                   # ⊂ ix_kg_k8s_job_kind_ns
    "ix_kg_storage_volumes_kind",            # ⊂ ix_kg_storage_volumes_kind_ns
    "ix_kg_volume_edges_src_kind",           # ⊂ ix_kg_volume_edges_src
    "ix_kg_volume_edges_dst_kind",           # ⊂ ix_kg_volume_edges_dst
    "ix_kg_services_namespace",              # ⊂ uq_kg_service_ns_name_kind
    "ix_kg_pod_events_namespace",            # ⊂ ix_kg_pod_event_ns_reason_time
    "ix_kg_k8s_jobs_namespace",              # ⊂ uq_kg_k8s_job_ns_name_kind
    "ix_kg_storage_volumes_kind_ns",         # ⊂ uq_kg_storage_volumes_kind_ns_name
    "ix_kg_action_approvals_incident_id",    # ⊂ uq_kg_action_approvals_incident_intent
    "ix_kg_remediation_decisions_incident_id",  # ⊂ uq_..._incident_idem
)

_DROP = _PK_DUPLICATES + _UNIQUE_DUPLICATES + _PREFIX_DUPLICATES

#: Как воссоздать снятое, если понадобится откат. Уникальность здесь ни у
#: кого не проверяется — это копии, и создаются они обычными индексами.
_RECREATE = {
    "ix_kg_action_approvals_id": ("kg_action_approvals", "id"),
    "ix_kg_alerts_id": ("kg_alerts", "id"),
    "ix_kg_anomaly_observations_id": ("kg_anomaly_observations", "id"),
    "ix_kg_cluster_observations_id": ("kg_cluster_observations", "id"),
    "ix_kg_deployments_id": ("kg_deployments", "id"),
    "ix_kg_ingress_observations_id": ("kg_ingress_observations", "id"),
    "ix_kg_k8s_jobs_id": ("kg_k8s_jobs", "id"),
    "ix_kg_log_observations_id": ("kg_log_observations", "id"),
    "ix_kg_pod_events_id": ("kg_pod_events", "id"),
    "ix_kg_remediation_decisions_id": ("kg_remediation_decisions", "id"),
    "ix_kg_service_edges_id": ("kg_service_edges", "id"),
    "ix_kg_service_health_id": ("kg_service_health", "id"),
    "ix_kg_services_id": ("kg_services", "id"),
    "ix_kg_signal_aggregates_id": ("kg_signal_aggregates", "id"),
    "ix_kg_storage_volumes_id": ("kg_storage_volumes", "id"),
    "ix_kg_volume_edges_id": ("kg_volume_edges", "id"),
    "ix_kg_service_health_service_ts": ("kg_service_health", "service_id, ts"),
    "ix_kg_signal_aggregates_service_window":
        ("kg_signal_aggregates", "service_id, window_end"),
    "ix_kg_cluster_obs_ts": ("kg_cluster_observations", "ts"),
    "ix_kg_remediation_decisions_incident_idem":
        ("kg_remediation_decisions", "incident_id, idempotency_key"),
    "ix_kg_service_health_service_id": ("kg_service_health", "service_id"),
    "ix_kg_anomaly_obs_service_ts": ("kg_anomaly_observations", "service_id, ts"),
    "ix_kg_anomaly_observations_service_id":
        ("kg_anomaly_observations", "service_id"),
    "ix_kg_signal_aggregates_service_id": ("kg_signal_aggregates", "service_id"),
    "ix_kg_log_observations_service_id": ("kg_log_observations", "service_id"),
    "ix_kg_deployments_service_id": ("kg_deployments", "service_id"),
    "ix_kg_pod_events_service_id": ("kg_pod_events", "service_id"),
    "ix_kg_alerts_service_id": ("kg_alerts", "service_id"),
    "ix_kg_k8s_jobs_kind": ("kg_k8s_jobs", "kind"),
    "ix_kg_storage_volumes_kind": ("kg_storage_volumes", "kind"),
    "ix_kg_volume_edges_src_kind": ("kg_volume_edges", "src_kind"),
    "ix_kg_volume_edges_dst_kind": ("kg_volume_edges", "dst_kind"),
    "ix_kg_services_namespace": ("kg_services", "namespace"),
    "ix_kg_pod_events_namespace": ("kg_pod_events", "namespace"),
    "ix_kg_k8s_jobs_namespace": ("kg_k8s_jobs", "namespace"),
    "ix_kg_storage_volumes_kind_ns": ("kg_storage_volumes", "kind, namespace"),
    "ix_kg_action_approvals_incident_id": ("kg_action_approvals", "incident_id"),
    "ix_kg_remediation_decisions_incident_id":
        ("kg_remediation_decisions", "incident_id"),
}


def _dialect() -> str:
    return op.get_bind().dialect.name


def upgrade() -> None:
    if _dialect() != "postgresql":
        return          # sqlite в тестах: индексы создаются из моделей
    with op.get_context().autocommit_block():
        for name in _DROP:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))


def downgrade() -> None:
    if _dialect() != "postgresql":
        return
    with op.get_context().autocommit_block():
        for name in _DROP:
            table, cols = _RECREATE[name]
            op.execute(sa.text(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} "
                f"ON {table} ({cols})"
            ))
