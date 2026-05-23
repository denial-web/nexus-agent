# Backup & Disaster Recovery — Nexus Agent (Postgres)

> **Scope.** Postgres-backed production deployments using the Docker Compose
> `prod` profile (`docker-compose.yml`). SQLite deployments use a simpler
> file-copy procedure documented in §6.
>
> **What this runbook protects.** All durable Nexus state: traces (with the
> tamper-evident hash chain), policies, approval logs, capability-token
> issuance records, critic registry, labeling queue, beliefs/memory,
> calibration snapshots, and skills. Redis state (rate limits, idempotency
> cache, in-flight capability tokens) is **ephemeral and not backed up** —
> rate limits reset cleanly, idempotency keys re-execute, in-flight tokens
> regenerate on next approval.

---

## Contents

1. [Targets — RPO and RTO](#1-targets--rpo-and-rto)
2. [What needs backing up](#2-what-needs-backing-up)
3. [Logical backups (pg_dump) — daily](#3-logical-backups-pg_dump--daily)
4. [Physical backups (pg_basebackup + WAL) — for PITR](#4-physical-backups-pg_basebackup--wal--for-point-in-time-recovery)
5. [ECDSA private key — must be backed up separately](#5-ecdsa-private-key--must-be-backed-up-separately)
6. [SQLite deployments](#6-sqlite-deployments)
7. [Restore procedure (logical backup)](#7-restore-procedure-logical-backup)
8. [Restore procedure (physical / PITR)](#8-restore-procedure-physical--pitr)
9. [Verification after restore](#9-verification-after-restore)
10. [Disaster scenarios](#10-disaster-scenarios)
11. [Off-site storage](#11-off-site-storage)

---

## 1. Targets — RPO and RTO

| Tier | RPO (data loss) | RTO (downtime) | Backup strategy |
|---|---|---|---|
| **Beta / internal** | ≤ 24h | ≤ 30 min | Daily `pg_dump` + ECDSA key file backup |
| **Production** | ≤ 15 min | ≤ 30 min | Daily `pg_dump` + WAL archiving for PITR |
| **High-availability** | ≤ 1 min | ≤ 5 min | Streaming replication + WAL archiving |

This runbook covers the first two tiers. HA streaming replication is out of
scope until v0.3 (multi-node deploy is not yet a supported profile).

---

## 2. What needs backing up

### Critical (loss = permanent data corruption)

| Asset | Where | Notes |
|---|---|---|
| Postgres database `nexus_db` | `pg-data` volume | All tables; hash-chained trace integrity depends on row order |
| ECDSA private key | `${ECDSA_PRIVATE_KEY_PATH}` | If lost, all existing capability-token signatures become unverifiable |
| `.env` | Host filesystem | `NEXUS_API_KEY`, `SESSION_SECRET`, `POSTGRES_PASSWORD`, reviewer IDs, provider keys |

### Recoverable from elsewhere (no backup needed)

- Redis state (rate limits, idempotency cache, in-flight tokens)
- Application container (built from git + Dockerfile)
- Alembic revisions (in git)
- Critic adapter weights (if used, store in object storage out-of-band)

---

## 3. Logical backups (pg_dump) — daily

Simplest reliable backup. Recommended baseline for every prod deployment.

### Take a backup

From the host running `docker compose`:

```bash
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR=/var/backups/nexus
mkdir -p "$BACKUP_DIR"

docker compose --profile prod exec -T postgres \
  pg_dump -U nexus -d nexus_db --format=custom --compress=9 \
  > "$BACKUP_DIR/nexus_db_${TIMESTAMP}.dump"

# Verify the file is not empty and Postgres-readable
docker compose --profile prod cp "$BACKUP_DIR/nexus_db_${TIMESTAMP}.dump" \
  postgres:/tmp/nexus_verify.dump
docker compose --profile prod exec -T postgres \
  pg_restore --list /tmp/nexus_verify.dump | head -8
docker compose --profile prod exec -T postgres rm -f /tmp/nexus_verify.dump
```

Expected output: header lines starting with `;` (`Archive created at …`,
`Format: CUSTOM`), then a table-of-contents listing. If `pg_restore`
errors with *did not find magic string*, the dump is corrupt or was
verified via stdin (Alpine `pg_restore` does not reliably read custom
format from `/dev/stdin` through `docker exec -T`). **Do not retain the
backup** if verification fails.

### Schedule (cron, host machine)

```cron
# Daily at 03:00 UTC
0 3 * * * /usr/local/bin/nexus-backup.sh >> /var/log/nexus-backup.log 2>&1
```

Where `/usr/local/bin/nexus-backup.sh` is the snippet above plus an
upload-to-off-site step (§11).

### Retention

Recommended baseline:

- Keep daily backups for **7 days**
- Keep weekly backups (Sunday) for **4 weeks**
- Keep monthly backups (1st of month) for **12 months**

Total ≈ 23 backups at any time. A typical Nexus database after 1 month of
moderate traffic is < 500 MB compressed.

---

## 4. Physical backups (pg_basebackup + WAL) — for point-in-time recovery

Required if your RPO is < 24 hours. Logical backups can only restore to the
exact time of `pg_dump`; physical + WAL replays let you restore to any
moment between base backups.

### One-time setup — enable WAL archiving

Add to `docker-compose.yml` under the `postgres` service:

```yaml
postgres:
  image: postgres:16-alpine
  command:
    - postgres
    - -c
    - archive_mode=on
    - -c
    - archive_command=test ! -f /wal-archive/%f && cp %p /wal-archive/%f
    - -c
    - wal_level=replica
    - -c
    - max_wal_senders=3
  volumes:
    - pg-data:/var/lib/postgresql/data
    - wal-archive:/wal-archive
  # ... rest unchanged

volumes:
  pg-data:
  wal-archive:
```

Restart Postgres after the change (during a maintenance window).

### Take a base backup

```bash
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR=/var/backups/nexus/base
mkdir -p "$BACKUP_DIR/$TIMESTAMP"

docker compose --profile prod exec -T postgres \
  pg_basebackup -U nexus -D - -Ft -X fetch -z \
  > "$BACKUP_DIR/$TIMESTAMP/base.tar.gz"
```

Schedule weekly. WAL segments accumulate in `/wal-archive` between base
backups and must be retained until the next base backup is verified.

### Prune old WAL segments

After a successful base backup and verification:

```bash
docker compose --profile prod exec -T postgres \
  pg_archivecleanup /wal-archive "$(latest_wal_after_previous_base)"
```

---

## 5. ECDSA private key — must be backed up separately

**Critical.** If you set `ECDSA_PRIVATE_KEY_PATH` (which you should — see
[PRODUCTION_READINESS.md](PRODUCTION_READINESS.md)), the key file is the
sole source of truth for verifying historical capability-token signatures.
Losing it means:

- Existing `governance_token_id` references in old traces become
  unverifiable
- The hash chain remains intact (it does not depend on ECDSA), but
  token-bound resume calls for in-flight approvals will fail

### Back it up

The PEM is tiny (~250 bytes) but must live in a different blast radius
than the Postgres backup:

```bash
# On a different host or an offline encrypted volume
cp /secrets/ecdsa.pem /backups/ecdsa-$(date -u +%Y%m%d).pem.bak
chmod 400 /backups/ecdsa-*.pem.bak
```

Do **not** check the key into git or include it in the `pg_dump` workflow.
A compromised Postgres backup that also contains the signing key
defeats the purpose of having signatures.

### Key rotation

If you need to rotate:

1. Generate the new key.
2. Stop traffic (or run with quorum=0 briefly during the swap window).
3. Update `ECDSA_PRIVATE_KEY_PATH` to point at the new file and restart.
4. **Retain the old key indefinitely** for verifying historical tokens.
5. New tokens are signed with the new key; old tokens remain verifiable
   only if the old key is still available to `verify_signature()`.

Multi-key support is not shipped yet — `app/core/covernor/token_manager.py`
loads exactly one key. If you rotate, retain audit-only access to the old
key via an out-of-band path until the oldest in-flight token has expired
(default TTL: 300 seconds).

---

## 6. SQLite deployments

For the default Docker Compose profile (SQLite, `nexus-data` volume):

```bash
# Online backup — uses sqlite3 .backup, no downtime
docker compose exec -T nexus \
  sqlite3 /app/data/nexus.db ".backup '/app/data/nexus-$(date -u +%Y%m%dT%H%M%SZ).db'"

# Copy out of the container volume
docker compose cp nexus:/app/data/nexus-*.db /var/backups/nexus/
```

SQLite has no WAL archiving in this configuration; RPO equals time since
the last `.backup` call.

---

## 7. Restore procedure (logical backup)

**Test this on a staging environment before you need it in anger.**

```bash
# 1. Stop the application — Postgres can stay running
docker compose --profile prod stop nexus-prod

# 2. Drop and recreate the database
docker compose --profile prod exec -T postgres \
  psql -U nexus -d postgres -c "DROP DATABASE nexus_db;"
docker compose --profile prod exec -T postgres \
  psql -U nexus -d postgres -c "CREATE DATABASE nexus_db OWNER nexus;"

# 3. Restore from the dump (copy into the container first — same stdin caveat as §3)
docker compose --profile prod cp /var/backups/nexus/nexus_db_20260523T030000Z.dump \
  postgres:/tmp/nexus_restore.dump
docker compose --profile prod exec -T postgres \
  pg_restore -U nexus -d nexus_db --no-owner --no-acl /tmp/nexus_restore.dump
docker compose --profile prod exec -T postgres rm -f /tmp/nexus_restore.dump

# 4. Run any pending Alembic migrations (the backup may predate a deploy)
docker compose --profile prod run --rm nexus-prod \
  alembic upgrade head

# 5. Restart the application
docker compose --profile prod up -d nexus-prod

# 6. Verify (see §9)
```

Estimated RTO: **5–15 minutes** for a database under 1 GB.

---

## 8. Restore procedure (physical / PITR)

Use only if you need to recover to a specific point in time between base
backups. Requires the WAL archive from §4.

```bash
# 1. Stop Postgres
docker compose --profile prod stop postgres nexus-prod

# 2. Wipe the data volume (this is the destructive step — be sure)
docker volume rm $(docker volume ls -q | grep pg-data)

# 3. Recreate Postgres with the base backup
docker compose --profile prod up -d postgres
docker compose --profile prod exec -T postgres bash -c '
  tar -xzf /backups/base/20260523T030000Z/base.tar.gz \
    -C /var/lib/postgresql/data/
  chown -R postgres:postgres /var/lib/postgresql/data
'

# 4. Configure PITR target (in /var/lib/postgresql/data/postgresql.conf)
docker compose --profile prod exec -T postgres bash -c '
  echo "restore_command = '"'"'cp /wal-archive/%f %p'"'"'" \
    >> /var/lib/postgresql/data/postgresql.conf
  echo "recovery_target_time = '"'"'2026-05-23 14:30:00 UTC'"'"'" \
    >> /var/lib/postgresql/data/postgresql.conf
  touch /var/lib/postgresql/data/recovery.signal
'

# 5. Restart and verify recovery completed
docker compose --profile prod restart postgres
docker compose --profile prod logs postgres | grep -i "recovery"
# Expected: "archive recovery complete" then "database system is ready"

# 6. Run migrations and restart app
docker compose --profile prod run --rm nexus-prod alembic upgrade head
docker compose --profile prod up -d nexus-prod
```

Estimated RTO: **15–60 minutes** depending on WAL volume to replay.

---

## 9. Verification after restore

Run these in order. Stop and investigate if any check fails before
re-opening traffic.

### 9.1 Basic connectivity

```bash
curl -fsS http://localhost:9000/health
curl -fsS http://localhost:9000/health/ready
```

Expected: both return `200`, readiness reports `database.connected: true`
and a matching pool size.

### 9.2 Hash chain integrity

```bash
curl -fsS -H "X-API-Key: $NEXUS_API_KEY" \
  http://localhost:9000/v1/traces?limit=1 | jq '.traces[0].id'

# For each session you care about, verify the chain
curl -fsS -H "X-API-Key: $NEXUS_API_KEY" \
  http://localhost:9000/v1/traces/session/<session_id>/verify-chain
```

Expected: `{"valid": true, "problems": []}`. If `valid: false`, inspect
`problems` — the restored database may be corrupted; restore from an
older backup.

### 9.3 Memory subsystem (if `MEMORY_ENABLED=true`)

```bash
curl -fsS -H "X-API-Key: $NEXUS_API_KEY" \
  http://localhost:9000/v1/memory/integrity
```

Expected: `{"chain_ok": true, "checked": N, "tampered": []}`.

### 9.4 Approval flow

```bash
nexus run "Write hello to nexus-restore-smoke.txt"   # expect pending_approval
nexus approve <approval_request_id>                  # uses reviewer-1
nexus resume <trace_id>                              # expect completed
```

If the ECDSA key was restored from a different file than the live one,
token signature verification fails here. Restore the matching key file or
accept that historical token-bound resumes won't validate.

### 9.5 Smoke test the benchmark suite

```bash
docker compose --profile prod run --rm nexus-prod \
  pytest tests/eval/skill_composition.py tests/eval/causal_qa.py \
    tests/eval/tool_injection_redteam.py -q
```

Expected: green. If red, the restore is functional but something semantic
is off — investigate before resuming production traffic.

---

## 10. Disaster scenarios

| Scenario | Action | Expected RTO |
|---|---|---|
| Postgres data volume corrupted | Restore from latest `pg_dump` (§7) | 15 min |
| Entire host lost | Spin up new host, pull backups from off-site (§11), run §7 | 30–60 min |
| Need data from yesterday morning (e.g. operator deleted policies) | Logical restore §7 to a sidecar DB, copy specific rows manually | 30 min |
| Need exact state at a specific minute | PITR restore §8 | 30–60 min |
| ECDSA key lost, traces remain | Generate new key; old governance_token_id values become unverifiable but traces stay readable and hash chain stays valid | 5 min + accept history loss |
| `.env` lost (no `NEXUS_API_KEY` / `SESSION_SECRET`) | Regenerate; existing dashboard sessions invalidate; clients update API key | 10 min + client cutover |

---

## 11. Off-site storage

Backups stored only on the same host as the database are not backups.

### Minimum bar (single off-site target)

```bash
# After §3 backup completes
aws s3 cp "$BACKUP_DIR/nexus_db_${TIMESTAMP}.dump" \
  "s3://nexus-backups-<account>/postgres/$(date -u +%Y/%m/%d/)" \
  --storage-class STANDARD_IA \
  --sse aws:kms
```

Equivalent options:

- Backblaze B2 + `rclone` (cheapest)
- GCS / Azure Blob via their CLIs
- A second host you control via `rsync` over SSH
- Self-hosted MinIO at a different physical location

### Verification cadence

- **Weekly:** download the most recent backup, run `pg_restore --list`,
  confirm it's intact.
- **Monthly:** perform a full restore to a disposable staging DB and run
  §9 verification.
- **Quarterly:** run a full DR drill — pretend the production host is
  gone, time the recovery on a fresh VM.

Untested backups are not backups.

### Dry-run validation (2026-05-23)

On a live `docker compose --profile prod` stack (Postgres 16, port 9001):

| Step | Result |
|---|---|
| `pg_dump` → 35 KB custom-format dump | Pass |
| `pg_restore --list` via `docker compose cp` into container | Pass (stdin via `exec -T` fails — documented above) |
| Sidecar restore to `nexus_db_drill` | Pass — trace count matched prod (3 = 3) |
| `/health`, `/health/ready` | Pass |
| `/v1/traces/session/{id}/verify-chain` | Pass — `valid: true` |

Full destructive restore (§7 stop app → drop `nexus_db` → restore) was **not**
run against the live stack; use a disposable staging host for that drill.

---

## See also

- [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md) — gap list and deploy checklist
- [beta_deploy_runbook.md](beta_deploy_runbook.md) — first prod boot, TLS, ECDSA key generation
- [Postgres backup docs](https://www.postgresql.org/docs/16/backup.html) — upstream reference
