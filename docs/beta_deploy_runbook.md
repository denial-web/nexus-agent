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

For Docker Compose prod, put these in `.env` **or** export them in the shell before
`docker compose --profile prod up`. The `nexus-prod` service passes
`SESSION_SECRET`, `APPROVAL_REVIEWERS`, `APPROVAL_QUORUM`, and `NEXUS_API_KEY`
through explicitly so production boot fails fast when they are missing.

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
GUNICORN_WORKERS=1 APPROVAL_QUORUM=1 APPROVAL_REVIEWERS=smoke-reviewer \
  docker compose --profile prod up --build -d postgres redis nexus-prod
```

Export `SESSION_SECRET`, `NEXUS_API_KEY`, and `POSTGRES_PASSWORD` in the same shell
(or put them in `.env`) before running the command above.

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

Completed on `master` as of 2026-05-23:

- Streaming zero-trust default (`STREAM_ZERO_TRUST_MODE=buffered`) — tokens withheld
  until critic, governance, and output scans pass.
- Full-record trace audit hash (`full_record_hash`) — governance, critic, scan,
  model, error, and agent metadata tamper detection.
- `web_fetch` SSRF hardening — private/loopback/link-local ranges, DNS-to-private
  resolution, redirect validation.
- Pending-approval response withholding on `/v1/agent/run` and agent endpoints.
- `/v1/agent/compare` returns no winner when all candidates are halted or
  output-blocked.
- Redis-backed capability token store — shared across gunicorn workers when
  `REDIS_URL` is set.
- Production config validator warnings for unset/missing `ECDSA_PRIVATE_KEY_PATH`.
- Public agent API redaction of `pending_tool` arguments and messages during
  `pending_approval`.

Remaining before public launch (non-blocking for beta smoke):

- Hosted deploy target confirmation (Fly/Railway/VPS) — TLS template in §10 below.
- Show HN / launch copy final review (drafts live in `~/nexus-launch-drafts/`).
- Optional: redact halted/blocked candidate bodies in compare responses.

## 10. TLS Termination (required for internet-facing prod)

Nexus listens on plain HTTP inside the container (`9000`). Do **not** expose that
port directly to the public internet. Terminate TLS at a reverse proxy.

### Caddy (simplest)

```caddy
nexus.example.com {
    reverse_proxy nexus-prod:9000
}
```

### nginx

```nginx
server {
    listen 443 ssl http2;
    server_name nexus.example.com;

    ssl_certificate     /etc/ssl/certs/nexus.fullchain.pem;
    ssl_certificate_key /etc/ssl/private/nexus.key;

    location / {
        proxy_pass http://127.0.0.1:9000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

After TLS is live, smoke through the public URL:

```bash
curl -fsS https://nexus.example.com/health
curl -fsS -H "X-API-Key: $NEXUS_API_KEY" https://nexus.example.com/v1/governance/approvals
```

### Persistent ECDSA signing key

Generate once and mount into every `nexus-prod` container:

```bash
openssl ecparam -genkey -name prime256v1 -noout -out ecdsa.pem
chmod 600 ecdsa.pem
```

Set in `.env`:

```bash
ECDSA_PRIVATE_KEY_PATH=/secrets/ecdsa.pem
```

Mount the host file into the container (example compose override):

```yaml
services:
  nexus-prod:
    volumes:
      - ./ecdsa.pem:/secrets/ecdsa.pem:ro
```

Startup warns in production when `ECDSA_PRIVATE_KEY_PATH` is unset; boot fails
when the path is set but the file is missing.
