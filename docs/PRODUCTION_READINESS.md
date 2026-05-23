# Production Readiness — Nexus Agent

> **Last reviewed:** 2026-05-23 (production hardening pass)  
> **Test signal:** 1439 passing (`pytest tests/ -q`)  
> **Verdict:** Ready for **controlled beta / early adopters** with Postgres + Redis + secrets. **Multi-worker prod supported when `REDIS_URL` is set.** Not GA for unrestricted multi-tenant without external audit.

---

## Summary

| Profile | Ready? |
|---|---|
| Local / dev (SQLite, mock LLM) | Yes |
| Single-worker prod (Postgres + Redis + secrets + quorum ≥ 2) | **Beta-ready** |
| Multi-worker prod (`GUNICORN_WORKERS > 1` + `REDIS_URL`) | **Beta-ready** (shared capability tokens, idempotency, rate limits) |
| Internet-facing SaaS / multi-tenant | Not yet |
| Regulated / compliance-heavy | Not yet |

Nexus has unusually strong security engineering for an OSS agent runtime (default-deny governance, 11-language injection scan, hash-chained audit, 1400+ tests). Operational maturity and multi-worker state sharing lag the security story.

---

## What is production-solid

- **7-step zero-trust pipeline** — input scan → decision → generation → critic → governance → output scan → hash-chained trace; every step tested.
- **Config validator** — refuses production boot without `NEXUS_API_KEY`, `SESSION_SECRET`, and `APPROVAL_REVIEWERS`.
- **Docker prod profile** — Postgres 16 + Redis 7 + explicit env wiring (`docker-compose.yml` `--profile prod`).
- **P0 hardening on `master`** — buffered streaming zero-trust, `full_record_hash`, `web_fetch` SSRF hardening, approval response withholding, `/compare` winner fix.
- **Operational primitives** — `/health`, `/health/ready`, graceful shutdown, rate limiting, idempotency keys, SIEM audit export, structured error envelope.
- **Beta smoke path** — documented in [beta_deploy_runbook.md](beta_deploy_runbook.md) (approve → resume → `completed`).
- **Redis-backed capability tokens** — shared across workers when `REDIS_URL` is set (`app/services/capability_token_store.py`).
- **Production config warnings** — validator warns on unset `ECDSA_PRIVATE_KEY_PATH`; errors on missing key file.
- **Pending-approval API redaction** — public agent payloads withhold `pending_tool` arguments, messages, and trajectory until approval completes.

---

## Gap list (prioritized)

### P0 — Remaining before external beta customers

| Gap | Impact | Mitigation today |
|---|---|---|
| **No TLS / hosted deploy runbook** | Raw HTTP exposure if operators skip reverse proxy | Terminate TLS at nginx/Caddy/Cloudflare ([beta_deploy_runbook.md](beta_deploy_runbook.md) §10) |
| **Ephemeral ECDSA key when `ECDSA_PRIVATE_KEY_PATH` unset** | Restart invalidates signing continuity | Set persisted PEM; startup warns in production |

### P0 — Shipped 2026-05-23

| Gap | Fix |
|---|---|
| ~~Capability tokens in-memory per worker~~ | Redis store via `REDIS_URL` |
| ~~`agent_state.pending_tool` exposed during `pending_approval`~~ | Redacted in `_public_agent_payload()` |

### P1 — Fix before calling it GA

| Gap | Impact |
|---|---|
| **No external security audit** | Claims are self-verified via test suite only |
| **Rate limiter fail-open on Redis reconnect** | Brief window where limits are not enforced |
| **No backup / DR runbook** | Beyond basic Docker rollback in beta runbook §8 |
| **MCP `require_approval` unsupported (v1)** | Returns JSON-RPC `-32001`; allow/deny only |

### P2 — Product maturity / positioning

| Gap | Impact |
|---|---|
| **OpenClaw import v1 limits** | Heuristic markdown→steps; no ClawHub slug API; no MCP auto-mapping ([openclaw_integration.md](openclaw_integration.md) § Limits) |
| **`MEMORY_ENABLED=false` by default** | Flagship memory is opt-in; document clearly for operators |
| **HF demo is mock governance only** | Perception gap vs full agent — README demo note addresses this |
| **LLM cache in-process** | Inconsistent across workers if `LLM_CACHE_ENABLED=true` |

---

## Minimum production checklist

Set before booting `nexus-prod`:

```bash
ENVIRONMENT=production
NEXUS_API_KEY=<strong-random-key>
SESSION_SECRET=<strong-random-secret>
APPROVAL_REVIEWERS=<reviewer-1>,<reviewer-2>   # real IDs, not examples
APPROVAL_QUORUM=2                               # use 1 only for first smoke, then restore
POSTGRES_PASSWORD=<strong-password>
DATABASE_URL=postgresql://nexus:${POSTGRES_PASSWORD}@postgres:5432/nexus_db
REDIS_URL=redis://redis:6379/0
GUNICORN_WORKERS=2                              # OK when REDIS_URL is set
ECDSA_PRIVATE_KEY_PATH=/secrets/ecdsa.pem       # generate once; mount persistent volume
```

Generate secrets:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Generate ECDSA key (once, persist the file):

```bash
openssl ecparam -genkey -name prime256v1 -noout -out ecdsa.pem
```

### Boot (Docker Compose prod)

```bash
GUNICORN_WORKERS=1 docker compose --profile prod up -d postgres redis nexus-prod
```

Use `GUNICORN_WORKERS=1` for the **first** boot on a fresh Postgres database so workers don't race Alembic migration setup. After `/health` is green, restart with `GUNICORN_WORKERS=2+` (Redis-backed state is shared safely across workers).

Start **explicit** prod services only — bare `docker compose --profile prod up -d` also starts the SQLite `nexus` service and can conflict on port binding. See [beta_deploy_runbook.md](beta_deploy_runbook.md) §3.

### Post-deploy smoke

```bash
curl -fsS http://<host>:9000/health
curl -fsS -H "X-API-Key: $NEXUS_API_KEY" http://<host>:9000/v1/governance/approvals

nexus run "Write hello to nexus-smoke.txt"    # expect pending_approval
nexus approve <approval_request_id>
nexus resume <trace_id>                       # expect completed
```

Full procedure: [beta_deploy_runbook.md](beta_deploy_runbook.md) §4–7.

### When scaling to multiple workers

Before increasing `GUNICORN_WORKERS`:

1. Set `REDIS_URL` (shared rate limits, idempotency, and capability tokens).
2. Set `NEXUS_SKIP_SCHEDULER=1` on all but one worker.
3. Mount a persistent `ECDSA_PRIVATE_KEY_PATH` volume shared by all workers.

---

## Multi-worker state matrix

| Subsystem | Single-worker | Multi-worker (with Redis) | Multi-worker gap |
|---|---|---|---|
| Rate limiting | In-process OK | Shared via Redis | Fail-open on Redis reconnect |
| Idempotency keys | In-process OK | Shared via Redis | — |
| ECDSA capability tokens | In-process OK | **Shared via Redis** | Redis outage → tokens unavailable (fail closed) |
| LLM response cache | In-process OK | Per-worker if enabled | Cache incoherence |
| Background scheduler | One instance OK | Needs `NEXUS_SKIP_SCHEDULER=1` on extras | Duplicate jobs if misconfigured |

---

## Abort / slip rules

- **L-60min smoke fails** (clone + eval pytest) → slip launch 24h. Do not ship with a broken quickstart.
- **Approval smoke fails** after deploy → stop traffic, inspect logs, do not widen access.
- **Schema rollback needed** → do not blind `alembic downgrade`; inspect history first ([beta_deploy_runbook.md](beta_deploy_runbook.md) §8).

---

## Road to GA (suggested order)

1. ~~Persist capability tokens (Redis) + ECDSA config-validator warning~~ — **done**
2. ~~Redact `pending_tool` arguments in public API payloads~~ — **done**
3. **Hosted deploy runbook** with TLS termination template — see [beta_deploy_runbook.md](beta_deploy_runbook.md) §10
4. **Structured pen test** or third-party audit; publish summary
5. **MCP `require_approval`** support (or document permanent allow/deny-only scope)
6. **Backup / restore** procedure for Postgres trace + policy tables

---

## See also

- [beta_deploy_runbook.md](beta_deploy_runbook.md) — first prod boot and approval smoke
- [fame_playbook.md](fame_playbook.md) — launch-day operations
- [benchmarks.md](benchmarks.md) — nightly exit-gated eval receipts
- [SECURITY.md](../SECURITY.md) — vulnerability reporting
