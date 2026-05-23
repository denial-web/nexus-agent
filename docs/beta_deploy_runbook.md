# Beta / Production Deployment Runbook

This runbook is for the first beta/prod deployment after the ClawGuard beta7
approval hardening. It assumes `master` is already green and synced.

## 1. Required Decisions

Do not deploy until these are known:

- **Target**: local Docker machine, remote SSH host, or hosted platform.
- **Reviewer IDs**: real IDs that are allowed to approve actions.
- **Smoke quorum**: temporary `APPROVAL_QUORUM=1` for first smoke, then restore
  real quorum.

Recommended first beta smoke:

```bash
APPROVAL_REVIEWERS=alice
APPROVAL_QUORUM=1
```

Recommended real beta/prod setting after smoke:

```bash
APPROVAL_REVIEWERS=alice,bob
APPROVAL_QUORUM=2
```

Use real reviewer IDs; `alice,bob` are examples only.

## 2. Required Environment

Set at minimum:

```bash
NEXUS_API_KEY=<strong-random-key>
SESSION_SECRET=<strong-random-secret>
APPROVAL_REVIEWERS=<real-reviewer-id-1>,<real-reviewer-id-2>
POSTGRES_PASSWORD=<strong-postgres-password>
ENVIRONMENT=production
```

Generate secrets locally:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

For single-key smoke only:

```bash
APPROVAL_QUORUM=1
APPROVAL_REVIEWERS=<one-real-reviewer-id>
```

Do not keep `APPROVAL_QUORUM=1` for real beta/prod operation unless that is an
explicit product decision.

## 3. Docker Prod Profile

Start explicit prod services. Do not start the whole profile:

```bash
GUNICORN_WORKERS=1 docker compose --profile prod up --build -d postgres redis nexus-prod
```

Avoid:

```bash
docker compose --profile prod up -d
```

The bare profile also starts the default SQLite `nexus` service, and both
`nexus` and `nexus-prod` bind `NEXUS_PORT`.

Use `GUNICORN_WORKERS=1` for the first boot on a fresh Postgres database so
multiple workers do not race Alembic migration setup. After `/health` is green,
restart with the normal worker count.

## 4. First-Boot Checks

```bash
curl -fsS http://<host>:9000/health
curl -fsS -H "X-API-Key: $NEXUS_API_KEY" http://<host>:9000/v1/governance/approvals
```

Expected:

- `/health` returns `200`.
- `/v1/governance/approvals` returns `200` with `{"requests": [...]}`.
- Same approvals request without `X-API-Key` returns `401`.

## 5. Approval Smoke

Use a file-write task because seeded policies require approval for `file_write`.

```bash
export NEXUS_URL=http://<host>:9000
export NEXUS_API_KEY=<strong-random-key>
export NEXUS_APPROVER_ID=<reviewer-id-listed-in-APPROVAL_REVIEWERS>

nexus run "Write hello to nexus-smoke.txt"
```

Expected:

- `status` is `pending_approval`.
- Response includes `approval_request_id`.
- Pending action is `file_write`.

Approve:

```bash
nexus approve <approval_request_id>
```

Resume:

```bash
nexus resume <trace_id>
```

Expected final status:

```text
completed
```

## 6. Negative Smoke

Duplicate vote with the same API key should fail:

```bash
nexus approve <same_approval_request_id>
```

Expected:

- `400` if the request is already approved, or
- `409` if the same authenticated identity tries to vote twice while pending.

If `APPROVAL_QUORUM=2`, use two distinct authenticated reviewer identities for
the positive path. A single API key cannot forge two reviewer votes.

## 7. Restore Real Quorum

If the first smoke used temporary single-key settings, update to real beta/prod
settings and restart:

```bash
APPROVAL_REVIEWERS=<real-reviewer-id-1>,<real-reviewer-id-2>
APPROVAL_QUORUM=2
```

Then repeat the approval smoke with two distinct reviewer identities.

## 8. Rollback

For Docker Compose:

```bash
docker compose --profile prod stop nexus-prod
git checkout <previous-known-good-sha>
GUNICORN_WORKERS=1 docker compose --profile prod up --build -d postgres redis nexus-prod
```

If the database schema has changed since the previous SHA, do not downgrade
blindly. Stop the app and inspect Alembic history first:

```bash
docker compose --profile prod exec nexus-prod alembic history
docker compose --profile prod exec nexus-prod alembic current
```

## 9. Known Follow-Ups

- The streaming endpoint still emits tokens before post-generation governance
  and output scans. Treat it as a separate hardening item before claiming strict
  zero-trust streaming.
- Trace hash coverage should be expanded to include governance, critic, scan,
  model, and error fields, or a second full-record audit hash should be added.
- `web_fetch` SSRF coverage should be audited for IPv6 loopback, RFC1918,
  metadata IPs, DNS rebinding, and redirects.
