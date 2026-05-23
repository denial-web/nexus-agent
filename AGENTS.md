# AGENTS.md — Nexus Agent

## What This Project Is

Nexus Agent is a **Zero-Trust & Self-Evolving AI Agent System**. It wraps LLM calls in strict security boundaries (input scanning, governance approval, output scanning) and uses a critic tree to evaluate every generation. Failures feed back into a labeling queue for continuous fine-tuning.

**Sister project**: [Doctrine Lab](../thinking-DT/doctrine-lab/) provides the dataset generation, curation, fine-tuning pipeline, and benchmark evaluation. Nexus Agent is the runtime; Doctrine Lab is the training factory.

**Doctrine Lab Milestone 1 (B.1 + B.2) is COMPLETE**: Doctrine Lab now has agent-safety eval tasks (`agent_safety`, `agent_reasoning`, `agent_governance`, `injection_resistance`) and a `POST /api/datasets/import` endpoint ready to receive Nexus failure traces.

## Tech Stack

- **Python 3.13**, FastAPI, SQLAlchemy 2.0, Alembic, Pydantic v2
- **Database**: SQLite (dev), PostgreSQL (prod)
- **AI providers**: Google Gemini (`google-genai`), OpenAI (`openai`), DeepSeek (OpenAI-compatible), Ollama (OpenAI-compatible local) — all in `requirements.txt`
- **Crypto**: `cryptography` (for ECDSA capability tokens)
- **Observability**: Prometheus metrics (`prometheus-client`), OpenTelemetry distributed tracing (`opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`), structured logging
- **Database drivers**: `psycopg2-binary` (PostgreSQL)
- **Testing**: pytest + httpx TestClient (dual SQLite + Postgres CI), 93-test adversarial red-team suite

## Project Structure

```
app/
├── main.py                 # FastAPI app, lifespan, migration runner, policy seeding
├── config.py               # Pydantic Settings (all configurable via .env)
├── db.py                   # SQLAlchemy engine, session, Base
├── agent/
│   ├── pipeline.py         # Full zero-trust pipeline orchestrator (THE CORE)
│   └── agent_loop.py       # run_agent(): tools, reflection, step traces, task reward
├── channels/
│   └── telegram_bot.py     # Optional Telegram long-poll when TELEGRAM_BOT_TOKEN set
├── cli.py                  # nexus CLI (chat, run, status, approve, feedback, benchmark)
├── api/
│   ├── agent.py            # /run, /stream, /compare, /agent/run, /agent/resume, /agent/feedback
│   ├── traces.py           # GET /v1/traces, GET /v1/traces/{id}/replay
│   ├── critic.py           # CRUD /api/critic/registry
│   ├── governance.py       # Policies, approvals, labeling queue
│   ├── skills.py           # CRUD /api/skills + execute + enable/disable
│   ├── training.py         # Labeling, export, eval, fine-tuning endpoints
│   └── dashboard.py        # Browser UI: traces, labeling, approvals, calibration, circuit breakers
├── services/
│   ├── integrity.py        # Hash-chain computation and verification
│   ├── replay.py           # Critic re-evaluation service
│   └── doctrine_bridge.py  # Doctrine Lab HTTP client
├── core/
│   ├── immune/scanner.py   # Multi-lang injection, memory bank, escalation, hardening
│   ├── asflc/
│   │   ├── engine.py       # A-S-FLC decision framework
│   │   └── analyzer.py     # LLM-powered path decomposition
│   ├── agent/              # Tool registry, builtins (shell, files, web, search)
│   ├── llm/
│   │   ├── models.py       # LLMResponse, LLMChunk dataclasses
│   │   └── provider.py     # Gemini/OpenAI/DeepSeek/Ollama/local/mock + generate_multi
│   ├── covernor/
│   │   ├── policy_engine.py    # Default-deny policy evaluation
│   │   └── token_manager.py    # ECDSA capability tokens
│   ├── critic/
│   │   ├── arbiter.py      # Central Arbiter (governs leaf nodes)
│   │   └── nodes.py        # Reasoning, Injection, Safety, Quality critics
│   ├── mcp/
│   │   ├── proxy.py       # GovernedMcpTool — immune + Covernor + trace
│   │   ├── config.py      # McpBackend registry JSON loader
│   │   └── server.py      # FastMCP entrypoints (streamable HTTP, stdio)
│   └── training/
│       ├── labeler.py      # Failure → labeling queue → training export
│       ├── calibration.py  # ECE (Expected Calibration Error) tracker
│       ├── evidential.py   # Uncertainty metadata enrichment for exports
│       └── scheduler.py    # Background auto-export + retention daemon
├── services/
│   ├── integrity.py        # Hash-chain computation and verification
│   ├── replay.py           # Critic re-evaluation service
│   ├── doctrine_bridge.py  # Doctrine Lab HTTP client
│   ├── rate_limiter.py     # Rate limiter backends (in-process + Redis)
│   ├── shutdown.py         # Graceful shutdown coordinator (drain + in-flight tracking)
│   └── config_validator.py # Startup config validation (errors + warnings)
│   ├── retention.py        # Scheduled data purge for old rows
│   ├── health_probe.py     # Deep health check — LLM provider reachability probes
│   ├── audit_export.py     # SIEM-compatible JSONL audit log export
│   ├── idempotency.py      # Idempotency key store (in-process LRU + Redis)
│   ├── provider_health.py  # Unified provider health (config + CB + probes)
│   └── webhooks.py         # HMAC-signed webhook dispatcher (async, retries)
├── errors.py               # Unified error envelope, NexusAPIError, exception handlers
├── version.py              # API version, project version, build metadata
├── tracing.py              # OpenTelemetry distributed tracing (optional, no-op when disabled)
├── logging_config.py       # JSON/text log formatters + request_id context var
├── sanitize.py             # Input sanitization (log injection, error message safety)
├── templates/              # Jinja2 HTML templates for dashboard
├── static/css/style.css    # Dashboard styles
└── models/
    ├── trace.py            # Append-only audit log (+ agent task fields)
    ├── step_trace.py       # Per-step agent audit rows
    ├── episode.py          # Reward-scored episodic memory
    ├── skill.py            # Auto-generated workflow skills with reward tracking
    ├── critic_registry.py  # Hot-swappable critic configs (CriticNode model)
    ├── policy.py           # Governance rules
    ├── approval_log.py     # K-of-N approval records + votes
    ├── labeling_queue.py   # Failure traces for fine-tuning
    └── webhook.py          # Webhook endpoint configs (URL, events, secret)
```

## Code Conventions

- **Imports at file top** — no inline imports except to avoid circular deps (those use `from X import Y` inside functions)
- **`datetime.now(timezone.utc)`** — never `datetime.utcnow()`
- **Pydantic v2** — use `model_config = ConfigDict(...)`, not `class Config`
- **SQLAlchemy 2.0** style — `Column()`, `declarative_base()`
- **Logging** — `logger = logging.getLogger(__name__)`, never `print()`
- **Tests** — pytest, `TestClient` for API tests, fixtures in `tests/conftest.py`. 1172 tests across 47 test files. CI runs against both SQLite and Postgres 16.
- **Alembic** — `render_as_batch=True` for SQLite, dialect-aware migrations (e.g., `USING` casts for Postgres). Migration smoke tests (`test_alembic_migrations.py`) verify upgrade/downgrade/upgrade cycle, stepwise apply/rollback of every revision, single-head chain integrity, and model-migration drift detection via `compare_metadata`.

## Pipeline Flow (app/agent/pipeline.py)

```
Prompt
  → Step 1: Immune input scan (multi-language injection detection, memory bank,
             escalation tracker; prompt hardening on FLAG verdicts)
  → Step 2: A-S-FLC decision analysis (LLM path decomposition → system hint)
  → Step 3: LLM generation (Gemini / OpenAI / DeepSeek / mock fallback)
  → Step 4: Arbiter critic evaluation (DB-backed + heuristic nodes)
      → If HALT: push to labeling queue, return error
  → Step 5: Covernor governance check (default-deny policy engine)
      → If DENY: return blocked
      → If REQUIRE_APPROVAL: create ApprovalRequest, issue ECDSA token on quorum
  → Step 6: Immune output scan (block leaked secrets)
  → Step 7: Return completed response + tamper-evident hash-chained trace
```

### Streaming Mode (`POST /v1/agent/stream`)

The same pipeline runs as SSE (Server-Sent Events). By default
(`STREAM_ZERO_TRUST_MODE=buffered`), LLM chunks are accumulated server-side;
critic/governance/output scans run on the accumulated response; token events are
emitted only after the response passes all post-generation checks. Set
`STREAM_ZERO_TRUST_MODE=preview` only for explicit non-zero-trust early-token
preview mode. Events:

- `event: status` — pipeline stage transitions (`input_scan`, `generating`, `evaluating`)
- `event: token` — individual LLM tokens (`{"text": "...", "index": N}`),
  emitted only after checks pass unless preview mode is enabled
- `event: done` — final result with trace_id, status, latency
- `event: error` — blocked/halted/error with reason

## Current State — All Phases Complete

**1172 passing tests** across 47 test files.

**Completed phases:**
- **Phase 1**: Foundation — pipeline, models, immune scanner, arbiter, governance, tests
- **Phase 2**: Live LLM integration — Gemini/OpenAI/DeepSeek/mock providers, streaming critic, `model_id`/`token_count` on traces
- **Phase 3**: DB-backed LLM critics — `Arbiter.load_from_registry()`, LLM reasoning/injection critics, re-evaluate endpoint
- **Phase 4**: Full governance — ECDSA tokens, K-of-N approval with quorum, hash-chained traces, approval expiration
- **Phase 5**: A-S-FLC integration — LLM path decomposition, asymmetric risk evaluation, system hints to guide generation
- **Phase 6**: Training flywheel — Doctrine Lab bridge, labeling API, ECE calibration, evidential loss enrichment, LoRA compare, scheduled export
- **Phase 7**: Agent-Immune upgrade — 11-language injection patterns, Semantic Memory Bank, PromptHardener, session escalation tracker
- **Phase 8**: Digital employee — `run_agent()` loop, built-in tools, reflection, `nexus` CLI, Telegram adapter, `StepTrace` + trace reward fields
- **Phase 9**: Learning — `Episode` memory, trajectory export (`export_agent_trajectories`), secure skill generation (`Skill` model, auto-abstract high-reward workflows, reward-tracked execution with auto-disable), skill recall in agent loop, skill CRUD API (`/api/skills`)
- **Phase 10**: Privacy — `LOCAL_ONLY`, Ollama routing; `OLLAMA_LIST_IN_PROVIDERS` (default off) so compare/auto-discovery does not probe localhost unless opted in
- **Phase 11**: MCP Governance Proxy + ClawHub Skill Import — governed MCP tool forwarding (immune scan + Covernor, namespaced `mcp:{backend}:{tool}` policies, hash-chained traces), backend registry CRUD API, skill SKILL.md import (file upload + URL), `instruction` step type, `nexus mcp serve/backends/add` + `nexus skills import` CLI
- **Dashboard**: Browser UI — trace explorer, labeling queue, approval console, calibration chart, circuit breaker monitor, unified provider health, skill import form
- **Multi-model compare**: `POST /api/agent/compare` — parallel LLM calls, per-candidate critic scoring, best-pick selection

## Running

### Local (development)
```bash
source venv/bin/activate
alembic upgrade head
uvicorn app.main:app --reload --port 9000
pytest tests/ -v
```

### Docker — SQLite (quick start)
```bash
cp .env.example .env          # fill in API keys
docker compose up -d
# → http://localhost:9000
```

### Docker — Postgres + Redis (production)
```bash
cp .env.example .env          # set NEXUS_API_KEY, SESSION_SECRET, APPROVAL_REVIEWERS, POSTGRES_PASSWORD, API keys
GUNICORN_WORKERS=1 docker compose --profile prod up -d postgres redis nexus-prod
# → First boot: Postgres 16, Redis 7, Nexus with one app worker
```

Use explicit prod services (`postgres redis nexus-prod`), not bare
`docker compose --profile prod up -d`, because the bare profile also starts
the default SQLite `nexus` service and can conflict on `NEXUS_PORT`. Use
`GUNICORN_WORKERS=1` for the first boot on a fresh Postgres database so
multiple gunicorn workers do not race Alembic migration setup; after `/health`
is green, restart with the normal worker count.

To run tests against PostgreSQL:
```bash
TEST_DATABASE_URL="postgresql://user:pass@localhost:5432/nexus_test" \
DATABASE_URL="postgresql://user:pass@localhost:5432/nexus_test" \
pytest tests/ -v
```

Dashboard: visit `http://localhost:9000/dashboard` after starting the server.

## Key Design Decisions

- **Default-deny governance**: Unknown actions are always denied. Policies must explicitly allow.
- **Critic tree, not flat scoring**: The Arbiter pattern allows adding/removing critic nodes at runtime via the DB.
- **Hash chain traces**: Each trace has `prev_hash` and `trace_hash` for tamper-evident audit logs.
- **[UNC] token insertion**: On rollback, the Arbiter inserts uncertainty markers during streaming evaluation.
- **ECDSA capability tokens**: After K-of-N approval, a signed single-use token is issued and verified cryptographically.
- **Prompt hardening**: Flagged (but not blocked) prompts have injection fragments stripped before LLM generation. Hardening patterns mirror all 11-language scanner patterns to prevent non-English bypass.
- **Memory-based detection**: Blocked attacks are added to the Semantic Memory Bank for fuzzy-matching future attempts.
- **Approval concurrency safety**: Approval votes use `SELECT … FOR UPDATE` row locks and a unique DB constraint on `(request_id, approver_id)` to prevent lost updates and duplicate votes under concurrency.
- **Session escalation**: Repeated suspicious prompts in the same session accumulate score until the session is auto-blocked.
- **Multi-model evaluation**: `POST /api/agent/compare` fires parallel LLM calls via `generate_multi()`, runs the Arbiter critic tree on each candidate, applies Covernor governance on the winner, and returns the highest-scoring viable response. Flagged prompts are hardened before generation. Timeout configurable via `COMPARE_TIMEOUT_SECONDS`.
- **SSE streaming**: `POST /api/agent/stream` runs the same zero-trust pipeline as SSE. Immune scan and hardening run synchronously before generation. With `STREAM_ZERO_TRUST_MODE=buffered` (default), generated chunks are held server-side until critic evaluation, governance check (including `require_approval`), and output scan pass, then token events are emitted. `STREAM_ZERO_TRUST_MODE=preview` is an explicit non-zero-trust early-token mode. CriticScore objects are serialized to JSON-safe dicts before trace persistence. Model ID and token counts are resolved from the provider route, not hardcoded.
- **A-S-FLC resilience**: `build_paths_from_llm_output` gracefully skips non-dict events and unconvertible values. The analyzer wraps path building in `try/except` and falls back to default paths on any structural error.
- **Dashboard vote error surfacing**: Dashboard vote endpoint returns an error page with the failure message (and back-link) instead of silently redirecting on error.
- **Secure-by-default metrics**: `EXPOSE_METRICS` defaults to `False`; operators must opt in via `.env`.
- **Structured logging**: JSON log output in production (machine-parseable for ELK/CloudWatch); human-readable text in development. Every log line includes `request_id`, `trace_id`, and `span_id` for full log-trace correlation. When OTel tracing is active, logs automatically carry the current trace/span context, enabling one-click jumps from log entries to distributed traces in Jaeger/Tempo/Datadog.
- **Request correlation**: `RequestIdMiddleware` assigns a unique ID to every request (or echoes the incoming `X-Request-ID` header). The ID is stored in a `contextvars.ContextVar` accessible from any logger, and returned in the `X-Request-ID` response header. When OTel tracing is active, the `X-Trace-ID` response header contains the OTel trace ID for frontend-to-backend trace correlation. The request ID propagates end-to-end: HTTP request → pipeline → LLM provider calls. OpenAI, DeepSeek, and Ollama calls include `X-Request-ID` as `extra_headers` on each API request. Every `LLMResponse` carries `request_id` for downstream audit. OTel spans include `request.id` attribute. LLM error logs include the request ID for grep-based correlation.
- **Skill recall**: Before the agent's first LLM call, `_retrieve_skills` keyword-matches enabled skills by description and injects their step sequences into the system prompt so the agent can follow proven workflows. Skills are ranked by `avg_reward`. A full REST API (`/api/skills`) provides list, get, execute, enable/disable, and delete operations — all Covernor-gated on execution.
- **Data retention**: Configurable per-table purge via `RETENTION_*_DAYS` env vars (0 = disabled). Runs every 12 scheduler cycles (~1 hour at default 5min interval). Only purges terminal-state rows (exported labeling items, resolved approvals) — never deletes pending work.
- **Multi-worker guard**: `NEXUS_SKIP_SCHEDULER=1` disables the background scheduler on secondary workers. Production startup logs a warning about in-process-only state (rate limits, tokens, scheduler).
- **MCP governance proxy**: When `MCP_ENABLED=true`, Nexus mounts a FastMCP Streamable-HTTP server at `/mcp` and exposes all registered backend tools as governed proxies. Each `tools/call` runs immune scan → Covernor policy check → forward → trace. Policies use namespaced `mcp:{backend}:{tool}` action IDs. v1 supports allow/deny only; `require_approval` returns JSON-RPC error `-32001`. `MCP_AUDIT_ALL=true` traces every call; `false` only traces denied/blocked calls. `LOCAL_ONLY=true` disables all MCP connections (HTTP routes return 503). CLI: `nexus mcp serve` (stdio), `nexus mcp backends`, `nexus mcp add <name> <url>`.
- **ClawHub skill import**: `POST /api/skills/import` accepts a SKILL.md file upload or URL. YAML frontmatter provides `name`, `description`, `metadata.openclaw.requires`. Markdown body is heuristically converted to Nexus steps (`tool_call`, `instruction`). `instruction` steps are skipped by `execute_skill()` but injected as LLM context during skill recall. Raw SKILL.md is stored in `raw_source` column. `GET /api/skills/{id}/source` returns it verbatim. Dedup by step hash. `LOCAL_ONLY` blocks URL imports.
- **Graceful shutdown**: On SIGTERM, the `ShutdownCoordinator` enters drain mode: new API requests receive 503 (`shutting_down`) with `Retry-After: 30`, while in-flight requests are allowed to complete up to `SHUTDOWN_DRAIN_SECONDS` (default 30s). Health probes (`/health`, `/health/ready`) and static assets remain accessible during drain so orchestrators can observe the shutdown. `/health` returns 503 with `status: "draining"` when drain is active. `/health/ready` includes `checks.shutdown.draining` and `checks.shutdown.in_flight` fields. The coordinator uses thread-safe reference counting with a `threading.Event` for efficient drain-wait without polling.
- **Structured error responses**: All HTTP errors (4xx/5xx) are normalized to a consistent JSON envelope with `error.code` (machine-readable), `error.message`, `error.status`, `error.request_id`, `error.timestamp`, and optional `error.trace_id` (when OTel active). A top-level `detail` field is preserved for backward compatibility. Validation errors (422) include `error.details.fields` with per-field breakdown. Custom `NexusAPIError` exception allows endpoints to raise errors with specific error codes. Pipeline responses (200 with status/error) follow the pipeline contract unchanged.
- **Request timeout**: Expensive endpoints (`/api/agent/run`, `/api/agent/agent/run`) wrap execution in a `ThreadPoolExecutor` with configurable `REQUEST_TIMEOUT_SECONDS` (default 120s, 0 = disabled). Timeout returns 504 with `error.code: "request_timeout"`.
- **API versioning**: All API endpoints are served under both `/v1/*` (canonical) and `/api/*` (legacy backward-compatible). Each API module defines its router with a domain prefix (e.g. `/agent`, `/traces`) and is mounted under two parent routers in `main.py`. Non-API routes (`/health`, `/health/ready`, `/dashboard`, `/metrics`, `/mcp`) remain unversioned. Rate limiting applies equally to both prefixes. The CLI uses `/v1/*` paths. When a v2 is needed, add a new parent router without disturbing v1 clients.
- **Legacy route deprecation**: `LegacyApiDeprecationMiddleware` adds RFC 8594 `Deprecation: true` to all `/api/*` responses, plus a `Link: </v1/...>; rel="successor-version"` header pointing to the canonical `/v1/` equivalent. When `API_LEGACY_SUNSET` is set to an ISO date (e.g. `2026-12-31`), a `Sunset` header is also included. `/v1/` responses carry none of these headers. Clients can detect and migrate programmatically.


## Operational Notes

- **Docker deployment**: Multi-stage Dockerfile (builder + runtime) with `gunicorn` + `uvicorn` workers. Default: 2 workers, configurable via `GUNICORN_WORKERS`. `docker-compose.yml` provides default SQLite service `nexus` and prod services (`postgres`, `redis`, `nexus-prod`). For prod, start explicit services (`docker compose --profile prod up -d postgres redis nexus-prod`) so the default SQLite service does not also bind `NEXUS_PORT`. On first boot against an empty Postgres database, set `GUNICORN_WORKERS=1` to avoid concurrent Alembic migration setup, then restart with the normal worker count after `/health` is green. Gunicorn's `--graceful-timeout` is wired to `SHUTDOWN_DRAIN_SECONDS` for coordinated shutdown. Container health check uses `curl` against `/health`.
- **Persist failure**: If the DB commit fails after LLM generation, the client gets a 500 and no trace is recorded. Monitor logs for `"Failed to persist trace"` — repeated occurrences indicate database connectivity or disk-space issues.
- **Rate limiting**: Applied to all expensive POST endpoints (`/api/agent/run`, `/api/agent/stream`, `/api/agent/compare`, `/api/training/lora/compare`, `/api/training/export`, `/dashboard/login`, etc.). Configurable via `RATE_LIMIT_RPM`. Backend: Redis sliding-window log (sorted set + Lua script, `ZRANGEBYSCORE` + `ZADD` + `EXPIRE`) when `REDIS_URL` is set (multi-worker safe, true sliding window — no fixed-window boundary burst), otherwise in-process memory. Auto-fallback: if Redis is unreachable at startup, falls back to in-process. Automatic reconnection: on runtime Redis failures, the backend attempts reconnect with a 5-second cooldown; fail-open during reconnect (requests allowed). Both backends return `RateLimitResult` with `remaining` count and `retry_after` seconds. Responses include `X-RateLimit-Limit`, `X-RateLimit-Remaining` headers; 429 responses additionally include `X-RateLimit-Reset` and `Retry-After`. Backend status visible in `GET /health/ready`.
- **Request body size limit**: `BodySizeLimitMiddleware` rejects requests exceeding `MAX_REQUEST_BODY_BYTES` (default 10 MB, 0 = disabled) with a structured 413 error before the body is parsed. Checks the `Content-Length` header for early rejection, preventing memory exhaustion from oversized payloads. Complements the per-prompt `MAX_PROMPT_LENGTH` check as defense-in-depth.
- **CORS hardening**: Disabled by default (same-origin only). When `CORS_ORIGINS` is set, uses explicit allow-lists for methods (`CORS_ALLOW_METHODS`, default `GET,POST,PUT,DELETE,OPTIONS`) and headers (`CORS_ALLOW_HEADERS`, default `Content-Type,X-API-Key,X-Request-ID,Authorization`). Wildcard `*` origin automatically disables `allow_credentials` (spec-forbidden combination). Preflight responses are cached via `CORS_MAX_AGE` (default 600s). Config validator warns on wildcard origins in production and origins missing `http(s)://` scheme.
- **Structured error envelope**: All HTTP error responses (4xx/5xx) return `{"error": {"code": "...", "message": "...", "status": N, "request_id": "...", "timestamp": "..."}, "detail": "..."}`. The envelope is enforced by four exception handlers (NexusAPIError, HTTPException, RequestValidationError, unhandled Exception) plus structured `_build_error_body` calls in all middleware error paths (auth, rate limit, body size, shutdown guard, MCP guard). `_STATUS_CODE_MAP` maps HTTP codes to machine-readable codes. 422 validation errors include per-field detail.
- **Database connection pool**: Configurable via `DB_POOL_SIZE` (default 5), `DB_MAX_OVERFLOW` (default 10), `DB_POOL_RECYCLE` (default 1800s, 0 = disabled), `DB_POOL_PRE_PING` (default true), `DB_POOL_TIMEOUT` (default 30s). Pool settings apply to PostgreSQL only; SQLite uses its default pool. `pool_pre_ping` issues a lightweight `SELECT 1` before handing out connections to detect stale/closed connections from load balancers or PgBouncer. The `/health/ready` endpoint exposes pool status (size, checked in/out, overflow) for monitoring.
- **Input sanitization**: `app/sanitize.py` provides `sanitize_for_log()` (escapes newlines/tabs, strips control chars, truncates — prevents log injection in text-mode logging) and `sanitize_for_error()` (strips control chars, replaces newlines, truncates, quotes — prevents information leakage in error messages). Applied to all user-supplied names in 409/400 error details (critic, governance, MCP APIs), DB-sourced names in log messages (dashboard), and external API responses reflected in errors. Raw exception details are never exposed to clients (MCP 502 returns a generic message; full details are logged server-side only).
- **Security headers**: `SecurityHeadersMiddleware` adds baseline headers to every response: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`, `X-Permitted-Cross-Domain-Policies: none`, `Permissions-Policy: camera=(), microphone=(), geolocation=(), interest-cohort=()`. Dashboard HTML pages get a strict CSP (`script-src 'none'`, `style-src 'self' 'unsafe-inline' fonts.googleapis.com`, `font-src fonts.gstatic.com`, `form-action 'self'`, `frame-ancestors 'none'`). API JSON responses get a minimal CSP (`default-src 'none'`). HSTS (`max-age=63072000; includeSubDomains`) is added in non-dev environments only.
- **Dashboard auth**: When `NEXUS_API_KEY` is set, `/dashboard` requires login via POST form. Session-based after first login. Query-string API key authentication is not supported (prevents credential leakage in logs/Referer headers). Unauthenticated in development mode.
- **API key rotation procedure**: (1) Generate a new key. (2) Set `NEXUS_API_KEY="new-key,old-key"` and restart. Both keys work; clients using the old key receive `X-API-Key-Deprecated: true`. (3) Migrate all clients to the new key. (4) Remove the old key from the env var and restart. Zero downtime throughout.
- **Startup config validation**: Comprehensive `config_validator.validate()` runs at boot and checks 7 categories: security (API key + session secret required in prod), contradictions (MCP + LOCAL_ONLY), numeric bounds (sample rates, thresholds, limits), timeout coherence (gunicorn ≥ request timeout ≥ drain), LLM provider availability, database suitability (SQLite in prod), and multi-worker consistency (Redis, scheduler). Fatal errors abort startup; warnings are logged. Generate secrets with: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

- **Health checks**: `GET /health` (liveness, always 200), `GET /health/ready` (readiness, 200 or 503). Readiness probes: database connectivity (with pool stats), LLM provider count, circuit breaker states (open circuits listed), LLM cache status (enabled, size, hit rate), OTel tracing status, rate limiter status, webhooks/MCP enabled flags, shutdown state, uptime. **Deep mode**: `GET /health/ready?deep=true` additionally probes each configured LLM provider with a lightweight connectivity test (list-models API call) running concurrently with per-provider timeout (`HEALTH_PROBE_TIMEOUT`, default 5s). Unreachable providers generate a warning in the response but do not degrade overall status — the deep probe is informational. Supports Gemini, OpenAI, DeepSeek, and Ollama. Use for Kubernetes liveness/readiness probes; enable deep mode for synthetic monitoring or pre-deployment verification.
- **Prometheus metrics**: Available at `GET /metrics` when `EXPOSE_METRICS=true` (opt-in, default `false`) and `prometheus_client` is installed. Protected by API key auth when `NEXUS_API_KEY` is set. Tracks pipeline latency, run counts by status, LLM call/error rates, critic scores, labeling queue depth, circuit breaker transitions, and LLM cache hit/miss rates. When enabled, `MetricsMiddleware` records per-request HTTP latency histograms (`nexus_http_request_duration_seconds`, labeled by method, normalized path template, status code), request counts by status class (`nexus_http_requests_total`), in-flight request gauge (`nexus_http_in_flight_requests`), and DB connection pool gauges (`nexus_db_pool_size`, `nexus_db_pool_checked_in`, `nexus_db_pool_checked_out`, `nexus_db_pool_overflow`). Path labels are normalized (dynamic segments collapsed to `{id}`) to prevent high-cardinality explosion.
- **API key comparison**: Uses timing-safe SHA-256 digest comparison (`_safe_key_compare`) to prevent length-based timing side-channels. Supports comma-separated keys in `NEXUS_API_KEY` for zero-downtime rotation: first key is primary, subsequent keys are secondary (being rotated out). Secondary keys return `X-API-Key-Deprecated: true` response header so clients know to update. All keys are accepted for authentication (including dashboard login). Config validator warns on duplicate keys.
- **Unicode normalization**: Input scanner runs dual-strategy Unicode normalization (strip + space-replace) to catch zero-width character insertion, fullwidth character substitution, Cyrillic/Latin homoglyph confusion, combining diacritic obfuscation, and invisible separator attacks. Confusable mapping covers 20+ Cyrillic→Latin and symbol homoglyphs.
- **Output scan precision**: Leak patterns require digit-containing values for unquoted secrets and quoted-string matching for quoted secrets, preventing false positives on code patterns like `apiKey = getApiKey()` while catching real leaks like `api_key=sk-abc123...`.
- **Red-team test suite**: 145 adversarial tests (`test_redteam.py`) covering 15 attack categories: structural injection, encoding/obfuscation evasion, multi-language advanced, indirect/contextual, compound/chained, output scan evasion, hardener edge cases, memory bank adversarial, false-positive resilience, scoring threshold boundaries, escalation tracker decay/lifecycle, output scan edge cases (quoted/unquoted/digit-requirement patterns), multi-language pattern completeness (every regex for all 11 languages), advanced encoding in non-English (homoglyphs + zero-width in CJK/Cyrillic/Romance), and pipeline-level integration (HTTP endpoint blocked-by-immune-scan round-trips).
- **Security benchmark**: `nexus benchmark` CLI command and `POST /api/agent/benchmark` endpoint. Runs 65 categorized attack payloads against the immune scanner, produces per-category detection rates and a composite security score. Supports `--json` for CI integration and `--threshold` for deployment gating (e.g. `nexus benchmark --threshold 0.95`).
- **CI pipeline**: Three parallel GitHub Actions jobs — `lint` (ruff, mypy, pip-audit), `test-sqlite`, `test-postgres` (service container with Postgres 16). The Postgres job catches dialect mismatches (e.g., implicit type casts) that SQLite silently accepts.
- **Circuit breaker**: Per-provider circuit breaker (`app/core/llm/circuit_breaker.py`) with CLOSED → OPEN → HALF_OPEN state machine. After `CB_FAILURE_THRESHOLD` failures within `CB_WINDOW_SECONDS`, the circuit opens and fast-fails requests. After `CB_RECOVERY_TIMEOUT` seconds, a probe request tests recovery. Automatic fallback chain: Gemini → OpenAI → DeepSeek → mock (configurable via `CB_FALLBACK_TO_MOCK`). Thread-safe with rolling failure window. Prometheus metrics: `nexus_circuit_breaker_state_changes_total`, `nexus_circuit_breaker_rejections_total`, `nexus_circuit_breaker_fallbacks_total`. **Dashboard visualization**: `/dashboard/circuit-breakers` shows all registered providers with state badges, failure meter bars, timing info (time since last failure, recovery countdown), and a manual reset button for open/half-open circuits. Enriched `get_status()` now returns `rolling_window_seconds`, `since_last_failure_seconds`, `recovery_remaining_seconds`, and `half_open_successes`.
- **LLM response cache**: Exact-match in-process LRU cache (`app/core/llm/cache.py`) keyed on `(prompt_hash, model_id, system_prompt_hash)`. Disabled by default (`LLM_CACHE_ENABLED=false`). Cached responses **still pass through** critic evaluation, governance, and output scan — only the LLM call is skipped. TTL eviction (`LLM_CACHE_TTL`, default 300s), max entries (`LLM_CACHE_MAX_ENTRIES`, default 1000). Thread-safe `OrderedDict` LRU. API: `GET /api/agent/cache/stats`, `DELETE /api/agent/cache`. Prometheus metrics: `nexus_llm_cache_hits_total`, `nexus_llm_cache_misses_total`. Use Redis for multi-worker deployments.
- **Webhook notifications**: HMAC-SHA256-signed HTTP POST notifications for system events (`app/services/webhooks.py`). Events: `approval_needed`, `critic_halt`, `circuit_open`, `input_blocked`, `output_blocked`, `export_complete`. Async dispatch via thread pool with exponential backoff + full jitter retry. Retryable conditions: connection errors, 5xx, 408 (Request Timeout), 429 (Too Many Requests). Non-retryable 4xx responses fail immediately without retry. Auto-disables webhook after N consecutive failures. Wildcard (`*`) subscription. CRUD API at `/api/webhooks` with test delivery endpoint. Config: `WEBHOOKS_ENABLED` (default `false`), `WEBHOOK_WORKERS` (default 2), `WEBHOOK_MAX_RETRIES` (default 3), `WEBHOOK_BACKOFF_BASE` (default 1.0s), `WEBHOOK_BACKOFF_MAX` (default 30.0s), `WEBHOOK_REQUEST_TIMEOUT` (default 10.0s), `WEBHOOK_MAX_CONSECUTIVE_FAILURES` (default 10). Backoff formula: `random(0, min(max_backoff, base * 2^attempt))` — full jitter prevents thundering-herd effects when multiple webhooks retry after a shared downstream outage.
- **Structured audit log export**: `GET /v1/traces/audit/export` streams SIEM-compatible JSON-lines (`application/x-ndjson`) for integration with Splunk, ELK, Datadog, etc. Each record follows a common envelope: `timestamp`, `event_type`, `severity`, `source`, `data`. Event types: `pipeline_run`, `input_blocked`, `output_blocked`, `critic_halt`, `governance_denied`, `approval_requested`, `approval_resolved`. Severity levels: `high` (blocked/leaked), `medium` (halted/denied), `info` (normal runs/approvals). Supports filters: `event_type` (multi-select), `since`/`until` (ISO datetime range), `status`, `limit`/`offset` (max 10 000). Also available as JSON array via `?format=json`. `GET /v1/traces/audit/events` lists available event types. Records include trace hash chain references for tamper-evidence verification downstream.
- **OpenTelemetry distributed tracing**: Optional OTel instrumentation (`app/tracing.py`) wraps all 7 pipeline steps and LLM provider calls with spans. Disabled by default (`OTEL_ENABLED=false`). When enabled, exports spans via OTLP/HTTP to any compatible collector (Jaeger, Tempo, Datadog). Root span `pipeline_run` records trace_id, session_id, final status, and latency. Child spans cover immune scan, A-S-FLC analysis, LLM generation (with cache hit/miss, provider, model, token count), critic evaluation, governance check, and output scan. LLM call attempts include per-attempt spans with retry tracking. No-op tracer fallback when SDK is absent or disabled — zero runtime cost. Config: `OTEL_SERVICE_NAME`, `OTEL_EXPORTER_ENDPOINT`, `OTEL_SAMPLE_RATE`. API: `GET /api/agent/tracing` returns status.
- **Concurrency safety**: Rate limiter uses `asyncio.Lock` for safe concurrent request counting. LLM client singletons use `threading.Lock` (double-checked locking) to prevent duplicate initialization under concurrent `generate_multi` threads. ECDSA key init likewise uses `threading.Lock`. Circuit breakers use per-instance `threading.Lock` for state transitions.
- **Capability token lifecycle**: Consumed tokens are immediately deleted from the in-memory store. When the store reaches 10 000 entries, expired and used tokens are evicted before new issuance.
- **Data retention**: Set `RETENTION_TRACE_DAYS`, `RETENTION_LABELING_DAYS`, `RETENTION_APPROVAL_DAYS`, `RETENTION_CALIBRATION_DAYS` to non-zero values to enable automatic purge. The scheduler runs retention every 12 cycles (~1h). Only terminal-state rows are purged (never pending work). For very large tables, consider DB partitioning as a complement.
- **Idempotency key support**: `POST /v1/agent/run` and `POST /v1/agent/compare` accept an `Idempotency-Key` header (8–256 chars). The first request with a given key is processed normally and the response is cached. Subsequent requests with the same key return the cached response without re-executing the pipeline, signaled by `X-Idempotent-Replayed: true` header. Two backends: `InProcessStore` (thread-safe LRU dict with TTL eviction, single-worker) and `RedisStore` (shared across workers when `REDIS_URL` is set). Config: `IDEMPOTENCY_TTL` (default 86400s = 24h), `IDEMPOTENCY_MAX_KEYS` (default 10000). Keys shorter than 8 or longer than 256 characters are rejected with 400 to prevent abuse. Middleware runs after auth and rate limiting, so rate limits still count and only authenticated clients can use idempotency. **In-flight deduplication**: If a second request with the same idempotency key arrives while the first is still processing (cache miss, not yet cached), it receives `409 Conflict` with `Retry-After: 2` instead of executing the pipeline concurrently. `InProcessStore` uses a thread-safe `set[str]` for in-flight tracking; `RedisStore` uses `SET key NX EX 300` for cross-worker distributed locking with 5-minute auto-expiry as a crash safety net. The in-flight lock is always released in a `finally` block.

- **Unified provider health**: `/dashboard/providers` combines configuration status, circuit breaker state, and optional live connectivity probes into a single at-a-glance view. Each of the four known providers (Gemini, OpenAI, DeepSeek, Ollama) is shown as a card with an overall status badge: `healthy` (configured + CB closed + probe reachable), `degraded` (CB half-open or probe unreachable), `down` (CB open), or `unconfigured`. The "Run Live Probes" button triggers `?probe=true` which executes real HTTP connectivity checks against each configured provider's API. Cards show default model, CB failure count, and probe latency/error. Open/half-open circuits can be reset directly from the provider card. JSON API: `GET /v1/agent/providers/health` (with optional `?probe=true`). Service layer: `app/services/provider_health.py` with `get_provider_health()` and `_compute_overall()`.

- **API versioning**: Every response includes an `X-API-Version` header (semver, e.g. `1.0.0`) set by `SecurityHeadersMiddleware`. Clients can send `Accept-Version` to signal their expected version; if it differs, the response includes `X-API-Version-Mismatch: true` for compatibility detection. `GET /v1/agent/version` returns full build metadata: `api_version`, `project_version`, `python_version`, `platform`, and `git_sha`. Version constants live in `app/version.py` — single source of truth.

See `PROJECT_PLAN.md` for full phase details, database schema reference, and API endpoint documentation.
