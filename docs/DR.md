# Disaster Recovery (DR)

Current as of v1.0.33 (2026-11-01). Namespace `sre-ai`.

## 1. What has to survive

| Asset | Where | Loss means |
|---|---|---|
| PostgreSQL, database from `postgres-secret` key `POSTGRES_DB` (~3.8 GB; largest table `kg_service_health` ~2.3 GB) | StatefulSet `postgres`, local-path PVC `postgres-data-postgres-0` (50Gi) on node `dev-4` | KG history: alerts, incidents, health series, deploys, remediation decisions / approvals / events |
| Redis | StatefulSet `redis`, local-path PVC `redis-data-redis-0` (10Gi) on node `dev-4`, AOF + hourly RDB | queues and short-lived windows only, see §5 |
| Secrets `sre-ai-secrets`, `postgres-secret`, `postgres-backup-s3` | cluster only, not in git | must be recreated by hand; `k8s/base/secrets.yaml` is a template |
| Manifests | git (`k8s/`), except the live hostname pin of `postgres` / `redis` to `dev-4` | — |

The audit log goes to stdout (`AUDIT_LOG_PATH` empty) and is not backed up by this project.

## 2. Backup

CronJob `postgres-backup` (`k8s/postgres-backup.yaml`), daily at **02:00 UTC**:

1. init container `pg-dump` (`postgres:15-alpine`): `pg_dump --format=custom --no-owner --no-privileges --compress=9`, then `gzip` → `/backup/sre-ai-<db>-<YYYYMMDDTHHMMSSZ>.sql.gz` on PVC `postgres-backup-data` (local-path 20Gi). Keeps the last `BACKUP_KEEP` dumps (default 7). The PVC lives on node `prod-shared`, i.e. not on `dev-4`.
2. container `s3-upload` (`amazon/aws-cli`): `aws s3 cp` to `s3://$BUCKET/postgres/` (`BUCKET` and credentials come from Secret `postgres-backup-s3`). The bucket lifecycle deletes objects after **7 days**. Without that Secret the Job stays green with the local copy only.

Note the file name: despite `.sql.gz` it is a gzipped **custom-format archive**, not SQL. Restore with `gunzip` + `pg_restore`, not `psql`. A dump is ~218 MB.

Alerts (`k8s/monitoring.yaml`): `CopilotPostgresBackupFailed` (failed Job), `CopilotPostgresBackupMissing` (no successful run for 26 h).

### Is the backup fresh?

```bash
kubectl -n sre-ai get cronjob postgres-backup            # LAST SCHEDULE
J=$(kubectl -n sre-ai get jobs -o name --sort-by=.metadata.creationTimestamp | grep postgres-backup | tail -1)
kubectl -n sre-ai logs $J -c pg-dump                     # "Dump complete: ...", "Kept: N dumps"
kubectl -n sre-ai logs $J -c s3-upload | tail -c 300     # "Backup complete: ..." (not "S3_BUCKET не задан")
aws s3 ls s3://$BUCKET/postgres/ | tail -3
```

A green Job alone does not prove the S3 copy exists — check the `s3-upload` log or the bucket listing.

## 3. Restore from a dump

Restore always goes into a stopped application: nothing must write during `pg_restore`.

```bash
kubectl -n sre-ai scale deploy sre-ai-api copilot-worker copilot-worker-sync \
  copilot-executor copilot-beat --replicas=0
```

### 3a. From S3

On a machine with credentials for the bucket:

```bash
aws s3 ls s3://$BUCKET/postgres/
F=sre-ai-<db>-<timestamp>.sql.gz
aws s3 cp s3://$BUCKET/postgres/$F .
gunzip -c $F > restore.dump
pg_restore --list restore.dump | head                    # archive is readable
```

Recreate the database and restore through stdin (no write access to the pod filesystem is needed):

```bash
kubectl -n sre-ai exec postgres-0 -- sh -c \
  'dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
kubectl -n sre-ai exec -i postgres-0 -- sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges' < restore.dump
```

### 3b. From the local copy (PVC `postgres-backup-data`)

The PVC is RWO local-path on `prod-shared`, so read it from a pod scheduled there (local-path pins the pod to the volume's node automatically). Recreate the database as in 3a, then:

```bash
kubectl -n sre-ai apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata: {name: pg-restore}
spec:
  restartPolicy: Never
  securityContext: {runAsUser: 999, runAsNonRoot: true, fsGroup: 999}
  containers:
  - name: restore
    image: postgres:15-alpine
    command: ["sleep", "3600"]
    env:
    - {name: PGHOST, value: postgres}
    - {name: PGUSER, valueFrom: {secretKeyRef: {name: postgres-secret, key: POSTGRES_USER}}}
    - {name: PGPASSWORD, valueFrom: {secretKeyRef: {name: postgres-secret, key: POSTGRES_PASSWORD}}}
    - {name: PGDATABASE, valueFrom: {secretKeyRef: {name: postgres-secret, key: POSTGRES_DB}}}
    volumeMounts: [{name: backup, mountPath: /backup, readOnly: true}]
  volumes:
  - {name: backup, persistentVolumeClaim: {claimName: postgres-backup-data}}
EOF
kubectl -n sre-ai exec pg-restore -- sh -c 'ls -1t /backup/*.sql.gz; cat /backup/LATEST'
kubectl -n sre-ai exec pg-restore -- sh -c \
  'gunzip -c /backup/$(cat /backup/LATEST) | pg_restore --no-owner --no-privileges -d "$PGDATABASE"'
kubectl -n sre-ai delete pod pg-restore
```

Do not run it while the CronJob is active (RWO volume, and the Job rotates files).

### 3c. After the restore

```bash
kubectl -n sre-ai exec postgres-0 -- sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT version_num FROM alembic_version"'
```

If the running image is newer than the dump's schema, run the migration Job first (see RUNBOOK → "If a deploy went out without migrations"). Then scale back up (api 2, worker 2, worker-sync 1, executor 1, beat 1) and check `/readyz`, the self-health report, and that an alert produces a KG record / card.

## 4. Node `dev-4` is lost

`postgres` and `redis` cannot move: their local-path volumes are on `dev-4`, and the live StatefulSets carry a `kubernetes.io/hostname In [dev-4]` term that git does not have. Pods stay `Pending`.

1. Make sure the node is really gone (not a short `NotReady`): a node that comes back brings the data back.
2. Stop the application (§3).
3. Pick another node with label `env=dev` and repoint both StatefulSets — `kubectl patch`, not `apply`:
   ```bash
   for s in postgres redis; do
     kubectl -n sre-ai get sts $s -o jsonpath='{.spec.template.spec.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions}'; echo
   done
   # replace the hostname value (index of the kubernetes.io/hostname expression, 1 at the time of writing):
   kubectl -n sre-ai patch sts postgres --type=json -p \
     '[{"op":"replace","path":"/spec/template/spec/affinity/nodeAffinity/requiredDuringSchedulingIgnoredDuringExecution/nodeSelectorTerms/0/matchExpressions/1/values","value":["<new-node>"]}]'
   kubectl -n sre-ai patch sts redis --type=json -p '<same patch>'
   ```
4. Drop the dead claims and pods so the `volumeClaimTemplates` create new local-path volumes on the new node:
   ```bash
   kubectl -n sre-ai delete pvc postgres-data-postgres-0 redis-data-redis-0 --wait=false
   kubectl -n sre-ai delete pod postgres-0 redis-0 --force --grace-period=0
   ```
   The old PVs may stay behind until the node is removed; clean them up afterwards.
5. Postgres initialises an empty database from `postgres-secret`. Restore it (§3a or §3b — the local copy is on `prod-shared`, so it survives the loss of `dev-4`).
6. Redis starts empty; nothing to restore (§5).
7. Update the comments in `k8s/postgres.yaml`, `k8s/redis.yaml` and the RUNBOOK that name `dev-4`.

## 5. What Redis holds

Nothing that must be restored. Losing it costs:

- Celery broker and results: queued tasks are lost; beat schedules periodic tasks again on the next tick. Apply tasks in the `executor` queue are lost — an approved but not executed action has to be clicked again (approvals themselves are in Postgres, `kg_action_approvals`).
- Suppression / dedup windows (`copilot:fire_dedup:*`, chronic-alert suppression, remediation bridge enqueue and proposal dedup): repeated cards are possible for one window; the bridge proposal dedup falls back to `kg_remediation_decisions`.
- Shared anti-replay nonce store for Discord interactions: degrades to the per-process cache.
- Digest snapshots (TTL ~25 h): the next daily digest may lack the comparison with the previous day.
- `ApprovalManager` state of the LLM path (disabled in production).

## 6. RPO / RTO (estimates, not measured)

- **RPO ≈ 24 h.** One dump a day at 02:00 UTC; a failure just before the next run loses almost a day of history (alerts, incidents, health points, decisions). Topology and current state are rebuilt by the regular KG syncs; history is not. If the last upload silently failed, `CopilotPostgresBackupMissing` fires only after 26 h, so the S3 copy can be up to ~2 days old — check §2 before relying on it.
- **RTO ≈ 1–2 h**, of which the restore itself is the unknown: download of ~218 MB plus `pg_restore` of a ~3.8 GB database with index rebuilds. Losing `dev-4` adds the StatefulSet repointing (§4). No restore drill has been done yet; record real timings here after the first one.
- **Retention:** 7 local dumps + 7 days in S3. Anything older is gone.

## 7. Drill

Restore the latest dump into a scratch database in the same instance, compare row counts of a few large tables, drop it:

```bash
kubectl -n sre-ai exec postgres-0 -- sh -c 'createdb -U "$POSTGRES_USER" dr_drill'
kubectl -n sre-ai exec -i postgres-0 -- sh -c \
  'pg_restore -U "$POSTGRES_USER" -d dr_drill --no-owner --no-privileges' < restore.dump
kubectl -n sre-ai exec postgres-0 -- sh -c 'psql -U "$POSTGRES_USER" -d dr_drill -tAc \
  "SELECT count(*) FROM kg_services; SELECT version_num FROM alembic_version"'
kubectl -n sre-ai exec postgres-0 -- sh -c 'dropdb -U "$POSTGRES_USER" dr_drill'
```

The application keeps running during the drill. Time it and update §6.
