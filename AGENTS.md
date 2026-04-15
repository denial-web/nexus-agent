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
- **Observability**: Prometheus metrics (`prometheus-client`), structured logging
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
│   ├── traces.py           # GET /api/traces, GET /api/traces/{id}/replay
│   ├── critic.py           # CRUD /api/critic/registry
│   ├── governance.py       # Policies, approvals, labeling queue
│   ├── skills.py           # CRUD /api/skills + execute + enable/disable
│   ├── training.py         # Labeling, export, eval, fine-tuning endpoints
│   └── dashboard.py        # Browser UI: traces, labeling, approvals, calibration
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
│   └── retention.py        # Scheduled data purge for old rows
├── logging_config.py       # JSON/text log formatters + request_id context var
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
    └── labeling_queue.py   # Failure traces for fine-tuning
```

## Code Conventions

- **Imports at file top** — no inline imports except to avoid circular deps (those use `from X import Y` inside functions)
- **`datetime.now(timezone.utc)`** — never `datetime.utcnow()`
- **Pydantic v2** — use `model_config = ConfigDict(...)`, not `class Config`
- **SQLAlchemy 2.0** style — `Column()`, `declarative_base()`
- **Logging** — `logger = logging.getLogger(__name__)`, never `print()`
- **Tests** — pytest, `TestClient` for API tests, fixtures in `tests/conftest.py`. 626 tests across 30 test files. CI runs against both SQLite and Postgres 16.
- **Alembic** — `render_as_batch=True` for SQLite, dialect-aware migrations (e.g., `USING` casts for Postgres)

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

### Streaming Mode (`POST /api/agent/stream`)

The same pipeline runs as SSE (Server-Sent Events). LLM tokens stream to the
client in real-time, then critic/governance/output scans run on the accumulated
response. Events:

- `event: status` — pipeline stage transitions (`input_scan`, `generating`, `evaluating`)
- `event: token` — individual LLM tokens (`{"text": "...", "index": N}`)
- `event: done` — final result with trace_id, status, latency
- `event: error` — blocked/halted/error with reason

## Current State — All Phases Complete

**626 passing tests** across 30 test files.

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
- **Dashboard**: Browser UI — trace explorer, labeling queue, approval console, calibration chart, skill import form
- **Multi-model compare**: `POST /api/agent/compare` — parallel LLM calls, per-candidate critic scoring, best-pick selection

## Running

```bash
source venv/bin/activate
alembic upgrade head
uvicorn app.main:app --reload --port 9000
pytest tests/ -v
```

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
- **SSE streaming**: `POST /api/agent/stream` runs the same zero-trust pipeline but streams LLM tokens as SSE events. Immune scan and hardening run synchronously before generation. Critic evaluation, governance check (including `require_approval`), and output scan run on the accumulated response after all tokens arrive. CriticScore objects are serialized to JSON-safe dicts before trace persistence. Model ID and token counts are resolved from the provider route, not hardcoded.
- **A-S-FLC resilience**: `build_paths_from_llm_output` gracefully skips non-dict events and unconvertible values. The analyzer wraps path building in `try/except` and falls back to default paths on any structural error.
- **Dashboard vote error surfacing**: Dashboard vote endpoint returns an error page with the failure message (and back-link) instead of silently redirecting on error.
- **Secure-by-default metrics**: `EXPOSE_METRICS` defaults to `False`; operators must opt in via `.env`.
- **Structured logging**: JSON log output in production (machine-parseable for ELK/CloudWatch); human-readable text in development. Every log line includes a `request_id` for correlation.
- **Request correlation**: `RequestIdMiddleware` assigns a unique ID to every request (or echoes the incoming `X-Request-ID` header). The ID is stored in a `contextvars.ContextVar` accessible from any logger, and returned in the `X-Request-ID` response header.
- **Skill recall**: Before the agent's first LLM call, `_retrieve_skills` keyword-matches enabled skills by description and injects their step sequences into the system prompt so the agent can follow proven workflows. Skills are ranked by `avg_reward`. A full REST API (`/api/skills`) provides list, get, execute, enable/disable, and delete operations — all Covernor-gated on execution.
- **Data retention**: Configurable per-table purge via `RETENTION_*_DAYS` env vars (0 = disabled). Runs every 12 scheduler cycles (~1 hour at default 5min interval). Only purges terminal-state rows (exported labeling items, resolved approvals) — never deletes pending work.
- **Multi-worker guard**: `NEXUS_SKIP_SCHEDULER=1` disables the background scheduler on secondary workers. Production startup logs a warning about in-process-only state (rate limits, tokens, scheduler).
- **MCP governance proxy**: When `MCP_ENABLED=true`, Nexus mounts a FastMCP Streamable-HTTP server at `/mcp` and exposes all registered backend tools as governed proxies. Each `tools/call` runs immune scan → Covernor policy check → forward → trace. Policies use namespaced `mcp:{backend}:{tool}` action IDs. v1 supports allow/deny only; `require_approval` returns JSON-RPC error `-32001`. `MCP_AUDIT_ALL=true` traces every call; `false` only traces denied/blocked calls. `LOCAL_ONLY=true` disables all MCP connections (HTTP routes return 503). CLI: `nexus mcp serve` (stdio), `nexus mcp backends`, `nexus mcp add <name> <url>`.
- **ClawHub skill import**: `POST /api/skills/import` accepts a SKILL.md file upload or URL. YAML frontmatter provides `name`, `description`, `metadata.openclaw.requires`. Markdown body is heuristically converted to Nexus steps (`tool_call`, `instruction`). `instruction` steps are skipped by `execute_skill()` but injected as LLM context during skill recall. Raw SKILL.md is stored in `raw_source` column. `GET /api/skills/{id}/source` returns it verbatim. Dedup by step hash. `LOCAL_ONLY` blocks URL imports.


## Operational Notes

- **Persist failure**: If the DB commit fails after LLM generation, the client gets a 500 and no trace is recorded. Monitor logs for `"Failed to persist trace"` — repeated occurrences indicate database connectivity or disk-space issues.
- **Rate limiting**: Applied to all expensive POST endpoints (`/api/agent/run`, `/api/agent/stream`, `/api/agent/compare`, `/api/training/lora/compare`, `/api/training/export`, `/dashboard/login`, etc.). Configurable via `RATE_LIMIT_RPM`. In-process memory only — use Redis for multi-worker deployments.
- **Dashboard auth**: When `NEXUS_API_KEY` is set, `/dashboard` requires login via POST form. Session-based after first login. Query-string API key authentication is not supported (prevents credential leakage in logs/Referer headers). Unauthenticated in development mode.
- **Production startup checks**: Both `NEXUS_API_KEY` and `SESSION_SECRET` must be set in non-development environments — the app refuses to start without them. Generate with: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- **Prometheus metrics**: Available at `GET /metrics` when `EXPOSE_METRICS=true` (opt-in, default `false`) and `prometheus_client` is installed. Protected by API key auth when `NEXUS_API_KEY` is set. Tracks pipeline latency, run counts by status, LLM call/error rates, critic scores, and labeling queue depth.
- **API key comparison**: Uses timing-safe SHA-256 digest comparison (`_safe_key_compare`) to prevent length-based timing side-channels.
- **Unicode normalization**: Input scanner runs dual-strategy Unicode normalization (strip + space-replace) to catch zero-width character insertion, fullwidth character substitution, Cyrillic/Latin homoglyph confusion, combining diacritic obfuscation, and invisible separator attacks. Confusable mapping covers 20+ Cyrillic→Latin and symbol homoglyphs.
- **Output scan precision**: Leak patterns require digit-containing values for unquoted secrets and quoted-string matching for quoted secrets, preventing false positives on code patterns like `apiKey = getApiKey()` while catching real leaks like `api_key=sk-abc123...`.
- **Red-team test suite**: 93 adversarial tests (`test_redteam.py`) covering 9 attack categories: structural injection, encoding/obfuscation evasion, multi-language advanced, indirect/contextual, compound/chained, output scan evasion, hardener edge cases, memory bank adversarial, and false-positive resilience across all 11 scanner languages.
- **Security benchmark**: `nexus benchmark` CLI command and `POST /api/agent/benchmark` endpoint. Runs 65 categorized attack payloads against the immune scanner, produces per-category detection rates and a composite security score. Supports `--json` for CI integration and `--threshold` for deployment gating (e.g. `nexus benchmark --threshold 0.95`).
- **CI pipeline**: Three parallel GitHub Actions jobs — `lint` (ruff, mypy, pip-audit), `test-sqlite`, `test-postgres` (service container with Postgres 16). The Postgres job catches dialect mismatches (e.g., implicit type casts) that SQLite silently accepts.
- **Circuit breaker**: Per-provider circuit breaker (`app/core/llm/circuit_breaker.py`) with CLOSED → OPEN → HALF_OPEN state machine. After `CB_FAILURE_THRESHOLD` failures within `CB_WINDOW_SECONDS`, the circuit opens and fast-fails requests. After `CB_RECOVERY_TIMEOUT` seconds, a probe request tests recovery. Automatic fallback chain: Gemini → OpenAI → DeepSeek → mock (configurable via `CB_FALLBACK_TO_MOCK`). Thread-safe with rolling failure window. Prometheus metrics: `nexus_circuit_breaker_state_changes_total`, `nexus_circuit_breaker_rejections_total`, `nexus_circuit_breaker_fallbacks_total`.
- **LLM response cache**: Exact-match in-process LRU cache (`app/core/llm/cache.py`) keyed on `(prompt_hash, model_id, system_prompt_hash)`. Disabled by default (`LLM_CACHE_ENABLED=false`). Cached responses **still pass through** critic evaluation, governance, and output scan — only the LLM call is skipped. TTL eviction (`LLM_CACHE_TTL`, default 300s), max entries (`LLM_CACHE_MAX_ENTRIES`, default 1000). Thread-safe `OrderedDict` LRU. API: `GET /api/agent/cache/stats`, `DELETE /api/agent/cache`. Prometheus metrics: `nexus_llm_cache_hits_total`, `nexus_llm_cache_misses_total`. Use Redis for multi-worker deployments.
- **Concurrency safety**: Rate limiter uses `asyncio.Lock` for safe concurrent request counting. LLM client singletons use `threading.Lock` (double-checked locking) to prevent duplicate initialization under concurrent `generate_multi` threads. ECDSA key init likewise uses `threading.Lock`. Circuit breakers use per-instance `threading.Lock` for state transitions.
- **Capability token lifecycle**: Consumed tokens are immediately deleted from the in-memory store. When the store reaches 10 000 entries, expired and used tokens are evicted before new issuance.
- **Data retention**: Set `RETENTION_TRACE_DAYS`, `RETENTION_LABELING_DAYS`, `RETENTION_APPROVAL_DAYS`, `RETENTION_CALIBRATION_DAYS` to non-zero values to enable automatic purge. The scheduler runs retention every 12 cycles (~1h). Only terminal-state rows are purged (never pending work). For very large tables, consider DB partitioning as a complement.

See `PROJECT_PLAN.md` for full phase details, database schema reference, and API endpoint documentation.
