# Disaster Recovery (DR) Plan

## 1. Critical Assets
- PostgreSQL (incidents, conversations, messages, feedback).
- Redis (Celery broker/backend, approval/session runtime data).
- Application configuration (`.env`, Kubernetes secrets).
- Audit logs (`AUDIT_LOG_PATH`).

## 2. Backup Strategy

### PostgreSQL
- Ежедневный logical backup (`pg_dump` / `pg_dumpall`).
- Хранение минимум 7–14 дней.
- Периодический тест восстановления на отдельный namespace/stand.

Пример:
```bash
kubectl exec -n copilot-prod postgres-0 -- pg_dumpall -U <db_user> > backup-$(date +%F).sql
```

### Redis
- Если Redis используется как кэш/эфемерный broker, RPO может быть relaxed.
- Для approval/session use-cases рекомендуется включить RDB/AOF snapshots.

### Kubernetes Manifests & Secrets
- Хранить манифесты в Git.
- Секреты — через external secret manager или зашифрованные SealedSecrets/SOPS.

## 3. Recovery Procedure
1. Развернуть инфраструктуру (PostgreSQL, Redis, API, workers).
2. Восстановить БД из последнего валидного backup.
3. Применить migrations (если нужны).
4. Проверить `/readyz` и тестовый webhook ingestion.
5. Проверить обработку Celery task end-to-end.

## 4. RTO/RPO Targets (Recommended)
- **RTO**: до 60 минут для восстановления базовой обработки инцидентов.
- **RPO**: до 24 часов для аналитических данных (или ниже, если инциденты критичны).

## 5. DR Drill Cadence
- Проводить DR-учения минимум 1 раз в квартал.
- Фиксировать фактический RTO/RPO и корректировать runbook.
