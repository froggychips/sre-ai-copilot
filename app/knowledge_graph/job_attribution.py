"""Атрибуция алертов про Job/CronJob владельцу, а не источнику метрики.

`KubeJobFailed` (и родня: KubeJobNotCompleted) несут лейбл `job_name`, а
лейбл `service` у них — `vm-kube-state-metrics`, откуда метрика приехала.
Store-путь резолвил цель только по deployment/statefulset/daemonset, и все
такие алерты падали на KSM: замер 07.09.2026 — 66 алертов за 30 дней, все
на `vm-kube-state-metrics`, включая «Job mcp/mcp-lastwar-wiki-sync-29779065
failed». Инциденты (1.0.7) эту атрибуцию унаследовали.

Граф уже знает владельцев: `kg_k8s_jobs.owner_service_name` (из labels
`app.kubernetes.io/part-of` и родни на Job/CronJob и pod-template) —
22 891 из 51 285 job'ов и 15 из 26 cronjob'ов. Порядок резолва:

  1. строка Job (ns, job_name) с owner_service_name → владелец;
  2. имя CronJob из имени Job (`<cronjob>-<unix-минуты, ≥8 цифр>`): строка
     CronJob с owner_service_name → владелец; без владельца → имя CronJob;
  3. без графа — имя CronJob по шаблону, иначе само имя Job.

Ответ всегда честнее KSM: даже «имя CronJob» указывает на объект, который
что-то запускал, а не на экспортер метрик. `how` в ответе говорит, какой
шаг сработал — это provenance атрибуции.
"""
from __future__ import annotations

import re
from typing import Any, Optional, Tuple

_CRONJOB_JOB_RE = re.compile(r"^(?P<base>.+?)-\d{8,}$")

HOW_JOB_OWNER = "job_owner"
HOW_CRONJOB_OWNER = "cronjob_owner"
HOW_CRONJOB_NAME = "cronjob_name"
HOW_JOB_NAME = "job_name"


def cronjob_base_name(job_name: str) -> Optional[str]:
    """`mcp-lastwar-wiki-sync-29779065` → `mcp-lastwar-wiki-sync`; без
    суффикса из ≥8 цифр (ad-hoc Job вроде `town-db-migrate`) → None."""
    m = _CRONJOB_JOB_RE.match(job_name or "")
    return m.group("base") if m else None


def resolve_job_target(db: Any, namespace: Optional[str], job_name: str) -> Tuple[Optional[str], str]:
    """(имя целевого сервиса, how). Никогда не возвращает KSM; пустое имя —
    только при пустом job_name."""
    if not job_name:
        return None, HOW_JOB_NAME
    base = cronjob_base_name(job_name)
    if db is not None and namespace:
        try:
            from app.knowledge_graph.schema import K8sJob
            row = (
                db.query(K8sJob.owner_service_name)
                .filter(K8sJob.namespace == namespace, K8sJob.name == job_name, K8sJob.kind == "job")
                .scalar()
            )
            if isinstance(row, str) and row:
                return row, HOW_JOB_OWNER
            if base:
                cron = (
                    db.query(K8sJob.owner_service_name, K8sJob.name)
                    .filter(K8sJob.namespace == namespace, K8sJob.name == base, K8sJob.kind == "cronjob")
                    .first()
                )
                if cron is not None and isinstance(cron[1], str):
                    owner = cron[0]
                    if isinstance(owner, str) and owner:
                        return owner, HOW_CRONJOB_OWNER
                    return base, HOW_CRONJOB_NAME
        except Exception:
            # Граф недоступен — деградируем к имени, а не к KSM.
            pass
    if base:
        return base, HOW_CRONJOB_NAME
    return job_name, HOW_JOB_NAME
