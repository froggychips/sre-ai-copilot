# Disaster Recovery (DR) Plan

## 1. PVC Snapshots (Velero)
Use **Velero** for infrastructure-level backups.
```bash
velero backup create copilot-daily --include-namespaces copilot-prod --snapshot-volumes
```

## 2. Database Backups
CronJob for daily `pg_dumpall`.
```bash
kubectl exec -it postgres-0 -- pg_dumpall -U sre_user > backup.sql
```

## 3. Restore Strategy
```bash
helm upgrade --install copilot ./charts/copilot --set image.tag=<known-good-sha>
```
