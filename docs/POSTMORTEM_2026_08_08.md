# Post-mortem 2026-08-08 — KG node type split and its fallout

A review of the `node_kind` rollout to production: what was fixed, what broke
in the process, and which measurements caught it. This document deliberately
records the **mistakes**, not just the outcome — most of them were invisible
to CI and only surfaced against live data.

Related PRs: #233 (node_kind), #234 (tests against a live DB), #236 (code
scanning), #238 (transactions + orphan), #239 (beat task `expires`).

---

## 1. The original problem

In `kg_services` a single node type meant two different things: the k8s
Service `auth` and the Deployment `auth` were **one row** — the unique key was
`(namespace, name)`.

It followed that the `serves_traffic` edge (Service → backing workload) could
not exist at all: it always came out as a self-loop and was discarded by a
guard. Measured on the live graph:

```
services_fetched=4342   skipped_self_loop=2092   skipped_no_match=2231
serves_traffic edges in the graph: 3
```

In the digest this read as a "dead edge" and inflated orphan counts — the data
lied not because a collector failed, but because of the storage model.

## 2. What was done

`kg_services.node_kind` (`service` / `workload` / `ingress`), unique key
widened to `(namespace, name, node_kind)`. The topology sync now creates
workload nodes itself and builds a real cross-node edge. Matching was extended
to StatefulSet and DaemonSet.

Production results after rollout:

| metric | before | after |
|---|---|---|
| `serves_traffic` | 3 | 4234 |
| `skipped_self_loop` per tick | 2092 | 0 |
| `skipped_no_match` per tick | 2231 | 6 |
| nodes in graph | 8669 | 11540 (service 8669 + workload 2871) |

---

## 3. Mistakes

### 3.1. Grep found half the sites, and nearly took down the hot path

We searched for node lookups by `(namespace, name)` using the pattern
`filter_by(namespace=…, name=…)`. Twelve were found and fixed. The form
`.filter(Service.namespace == …, Service.name == …)` — half the codebase —
did not match that pattern.

Among the misses were **eight `.one_or_none()` calls on the hot path**: alert
enrichment, blast radius, Discord embeds, remediation targets. Once a
same-named workload node appeared, each would raise `MultipleResultsFound` —
failing precisely during an incident, when the copilot is needed.

**How it was caught.** Not by grep, but by a test:
`test_no_new_node_lookup_without_node_kind` scans the sources for both forms.
It was written as a guard "for the future" and fired immediately, on existing
code.

**Takeaway.** When a change must cover every occurrence of a pattern, write
the completeness check as a test, not a one-off grep. Grep verifies what you
remembered; a test verifies what is in the code.

### 3.2. A migration without `lock_timeout` froze production for 6 minutes

`ALTER TABLE kg_services ADD COLUMN` needs ACCESS EXCLUSIVE. Workers were
holding long transactions, the lock never freed, and the `ALTER` queued up.
Then a PostgreSQL property that is easy to forget kicked in: **a waiting DDL
blocks everyone who arrives after it**. Seven `kg_services` readers piled up
behind it and the application stopped responding.

Deleting the Job did not help: a backend waiting on a lock never writes to its
socket, so it never notices the client is gone. It had to be cleared manually
with `pg_terminate_backend`.

**Takeaway.** Every DDL migration against a live database runs with
`lock_timeout`. Failing fast after N seconds is harmless; queueing drags
readers down with it. Implemented as `PGOPTIONS` in the migration Job — see
RUNBOOK.

### 3.3. Matching was extended, permissions were not

Matching StatefulSet/DaemonSet required new RBAC permissions. The repository
had them (`ClusterRole sre-ai-read` lists all three resources), so everything
looked ready. That role **does not exist in the cluster at all** — three other,
hand-assembled roles do. The sync failed:

```
statefulsets.apps is forbidden: User "system:serviceaccount:sre-ai:sre-ai" cannot list
```

The irony: without those permissions the unmatched workloads were exactly the
`*-db` / `*-postgresql` / clickhouse ones — the very nodes the extension was
built for.

**Takeaway.** "The permission is in the manifest" ≠ "the permission is in the
cluster". Verify with `kubectl auth can-i ... --as=system:serviceaccount:...`,
not by reading YAML.

### 3.4. `orphan_pct` started flattering us — and it was not noticed at first

After the rollout orphan dropped from 72.5% to 42.0%. The number was written
down as a result. A reviewer-requested check showed:

| counting | orphan |
|---|---|
| any edge (as before) | 2072 / 4933 → 42.0% |
| excluding `serves_traffic` | 3578 / 4933 → **72.5%** |

The second row is exactly the pre-rollout value. Inter-service connectivity
did not change at all: all 1506 "cured" services gained a single edge — to
their own workload.

The frustrating part is that this trap was explicitly guarded against:
`owner_pct` and `app_scope` count only `node_kind='service'`, so workload
nodes never diluted them. But `orphan` was defined as "has at least one edge",
and that guard did not cover it.

**Takeaway.** A metric must not improve because the storage schema changed.
When introducing a new node or edge type, re-examine **every** metric that
counts "anything at all", not only the ones where you expect an effect.
Locked down by `test_serves_traffic_alone_does_not_clear_orphan` and contract
2.5.

### 3.5. The "transaction leak" diagnosis was wrong

Observation: `idle in transaction`, ages up to 25 minutes. The conclusion was
hasty — "sessions are not being closed" — and
`idle_in_transaction_session_timeout=120s` was added.

Post-rollout measurement showed the timeout did nothing: transactions still
lived 12-13 minutes. The wrong thing had been measured:

```
transaction_age      idle_right_now
00:13:31             00:00:02
00:12:07             00:00:00.14
```

The transactions are **actively working**; the gaps between statements are
fractions of a second. `idle_in_transaction_session_timeout` targets idle
time, so it had nothing to cut. The real cause is long transactions: the sync
performs 4200+ upserts and commits once at the end, holding ACCESS SHARE
throughout.

**Takeaway.** `now() - xact_start` (transaction age) and `now() - state_change`
(current idle time) answer different questions. Locking cares about the first;
forgotten sessions about the second. The real fix is batched commits
(`_COMMIT_BATCH`); the timeout stays as insurance against genuinely forgotten
transactions.

### 3.6. A targeted `rollback` inside a library function

The first version of the fix added `db.rollback()` before `kubectl` inside
`sync_topology_resources`. The test
`test_sync_topology_resources_returns_both_slices` failed — rightly so: a
library function would have rolled back the **caller's uncommitted work**.

The check also showed the fix was unnecessary there: the session is fresh,
both `kubectl` calls happen before the first SQL, so no transaction is open
during the external calls.

**Takeaway.** Transaction control belongs to whoever owns the session. Before
"playing it safe", confirm the problem exists.

### 3.7. Small but telling

* **A pipe swallowed the exit code.** `pytest … | tail -3 && git push` — a
  failing test did not stop the push, because `tail` supplied the exit code.
  The branch shipped with a red test.
* **`kubectl exec` for a heavy job.** Running the sync by hand started a
  second Python process inside the worker pod on top of the running celery and
  hit the memory limit (`exit 137`). One-off heavy jobs get their own Job with
  their own resources.
* **The test itself misparsed a schedule.** `crontab(minute=43, hour="*/6")`
  read as "every 6 minutes" instead of "every 6 hours": the regex checked
  `minute` before `hour`.

---

## 4. Uncovered along the way (not about node_kind)

### 4.1. CI never ran 45 tests

With `DATABASE_URL` set, 45 additional tests became active — and failed. That
is why CI deliberately ran only two integration files.

One root cause covered 40 of them: dedup became cross-replica (the
`discord_dedup` table) while the tests only cleared the in-memory cache. The
row survived the run, the service took the PATCH path instead of POST, and the
test failed with `KeyError: 'payload'`.

Fixed in #234; CI now runs the full suite against postgres plus
`alembic upgrade head`.

### 4.2. The Celery queue piled up silently

The queue held 230 tasks, 94 of them `kg_external_probe` — a one-minute task
accumulated over an hour and a half. `expires` was set on exactly one task out
of 27.

The practical consequence: `kg_topology_resources_sync` was dispatched on
schedule at :15 and :30 and **never ran** — it sat behind the stale tail. After
the rollout the topology sync had to be triggered manually, otherwise
`serves_traffic` would have stayed at three edges with perfectly working code.

Fixed in #239.

### 4.3. Repository/cluster drift

Found three times in a single day:

| item | in the repository | in the cluster |
|---|---|---|
| image registry | `ghcr.io/froggychips/...` | `docker.lastoasisgame.com` (Nexus) |
| tag | `<git-sha>` | `1.0.0-rc.N` |
| RBAC | `ClusterRole sre-ai-read` | three other, hand-made roles |

`deploy.sh` from the repository has never worked against this cluster: it
deploys from `ghcr.io`, and no `imagePullSecrets` exist for it. The actual
rollout is a manual `kubectl set image` with a Nexus image.

**Not fixed.** Needs a decision: either bring `deploy.sh` and the manifests in
line with reality, or move the cluster onto the repository's process.

---

## 5. Final state

Genuine improvements:

* `serves_traffic` 3 → 4234, `skipped_no_match` 2231 → 6 — the
  Service→workload topology now exists and blast radius sees the backing
  workload;
* the API crash loop stopped: 0 restarts versus 103 over the preceding 14
  hours;
* CI runs the full suite against a live schema;
* the Celery queue no longer accumulates stale work.

Honest numbers:

* `orphan_pct` is back at 72.5% — inter-service connectivity is exactly what it
  was. That remains a high figure and a separate task.

Open threads:

* `deploy.sh` / RBAC / registry drift (see 4.3);
* `kg_ingress_observations_sync` issues thousands of VictoriaMetrics queries
  and occupies workers for a long time — `expires` protects its neighbours, but
  the task itself was not made faster.
