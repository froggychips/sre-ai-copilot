"""Набор правил deterministic-диагностики.

Каждое правило независимо: видит весь enriched context, отдаёт 0+ Fact-ов.
Добавить новое — отнаследовать Rule из base.py и заявить в DEFAULT_RULES.
"""
from app.diagnostics.rules.base import Rule
from app.diagnostics.rules.container_config import ContainerConfigRule
from app.diagnostics.rules.crashloop import CrashLoopBackOffRule
from app.diagnostics.rules.db_permission import DbPermissionRule
from app.diagnostics.rules.failed_scheduling import FailedSchedulingRule
from app.diagnostics.rules.migration_failed import MigrationFailedRule
from app.diagnostics.rules.image_pull import ImagePullRule
from app.diagnostics.rules.oom import OOMKilledRule
from app.diagnostics.rules.orleans_membership import OrleansMembershipRule
from app.diagnostics.rules.pod_events import PodEventsRule
from app.diagnostics.rules.process_crash import ProcessCrashRule
from app.diagnostics.rules.recent_deploy import RecentDeployRule
from app.diagnostics.rules.resource_pressure import ResourcePressureRule
from app.diagnostics.rules.upstream_degraded import UpstreamDegradedRule

DEFAULT_RULES: list[Rule] = [
    OOMKilledRule(),
    CrashLoopBackOffRule(),
    FailedSchedulingRule(),
    RecentDeployRule(),
    ResourcePressureRule(),
    UpstreamDegradedRule(),
    PodEventsRule(),
    ProcessCrashRule(),
    MigrationFailedRule(),
    DbPermissionRule(),
    OrleansMembershipRule(),
    ImagePullRule(),
    ContainerConfigRule(),
]

__all__ = [
    "Rule",
    "DEFAULT_RULES",
    "OOMKilledRule",
    "CrashLoopBackOffRule",
    "FailedSchedulingRule",
    "RecentDeployRule",
    "ResourcePressureRule",
    "UpstreamDegradedRule",
    "PodEventsRule",
    "ProcessCrashRule",
    "MigrationFailedRule",
    "DbPermissionRule",
    "OrleansMembershipRule",
    "ImagePullRule",
    "ContainerConfigRule",
]
