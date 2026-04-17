# Nexus Agent — Full Project Plan

## Vision

Build a production-grade AI agent system that merges a **Zero-Trust Agent Pipeline** (strict input/output boundaries) with a **Self-Improving, Risk-Aware Custom LLM** (critic-driven fine-tuning flywheel). The system must prove — with statistical evidence — that the fine-tuned model is safer and more capable than the base model.

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                       GATEWAY LAYER                              │
│  Agent-Immune Scanner ──→ [INPUT] ──→ Agent-Immune Scanner [OUT] │
├──────────────────────────────────────────────────────────────────┤
│                        BRAIN LAYER                               │
│  A-S-FLC Decision ──→ LLM Generation ──→ Arbiter Critic Tree    │
├──────────────────────────────────────────────────────────────────┤
│                     GOVERNANCE LAYER                             │
│  Covernor Policy Engine ──→ K-of-N Approval ──→ ECDSA Token     │
├──────────────────────────────────────────────────────────────────┤
│                      FLYWHEEL LAYER                              │
│  Failure Traces ──→ Labeling Queue ──→ Fine-Tune ──→ Deploy     │
└──────────────────────────────────────────────────────────────────┘
```

### Core Frameworks Integrated

| Framework | Role | Status |
|-----------|------|--------|
| **Agent-Immune** | Adaptive threat intelligence, semantic memory, 11-language injection detection | ✅ Complete (Phase 7) |
| **A-S-FLC** | Asymmetric Signed Force-Loop-Chain — risk-penalized decision engine | ✅ Complete (Phase 5) |
| **Covernor** | Default-deny policy engine, ECDSA tokens, K-of-N approval | ✅ Complete (Phase 4) |
| **GrokForge-Nexus v16** | Arbiter critic tree, chunked generate-then-verify, auto-rollback | ✅ Complete (Phase 2) |
| **Nexus Spin v5.3** | Lightweight causal transformer with memory loops, bilingual Khmer support | ✅ Complete — local HuggingFace provider via `model_id: local:repo/name` |
| **a-s-flc-decisions** | 806 structured A-S-FLC reasoning examples on HuggingFace | ✅ Import script ready (Phase 5) |

---

## Phase 1: Foundation ✅ COMPLETE

**What was built:**

### Database Models (6 tables)
- `traces` — append-only audit log with SHA-256 hash chain fields
- `critic_registry` — hot-swappable critic node configs (prompt templates, LoRA paths, weights)
- `policies` — Covernor governance rules (action patterns, decision, risk level)
- `approval_requests` — K-of-N approval tracking
- `approval_votes` — individual approver votes
- `labeling_queue` — failure traces awaiting human review for fine-tuning

### Core Modules
- `app/core/immune/scanner.py` — Input injection detection + output leak scanning + escalation phrases
- `app/core/asflc/engine.py` — Full A-S-FLC with EventNode, DecisionPath, convergence loop, chain regret calculation
- `app/core/covernor/policy_engine.py` — Default-deny with glob pattern matching
- `app/core/covernor/token_manager.py` — Single-use capability tokens (HMAC initial, upgraded to ECDSA in Phase 4)
- `app/core/critic/arbiter.py` — Central Arbiter with CriticNodeProtocol, register/unregister, max rollback enforcement
- `app/core/critic/nodes.py` — ReasoningCritic, InjectionCritic (can_halt), SafetyCritic (can_halt), QualityCritic
- `app/core/training/labeler.py` — push_failure, label_item, get_queue, export_for_training

### Pipeline & API
- `app/agent/pipeline.py` — Pipeline orchestration (mock LLM initially)
- Alembic migration generated and tested
- 4 default governance policies seeded on startup

---

## Phase 2: Live LLM Integration ✅ COMPLETE

**What was built:**
- `app/core/llm/provider.py` — Unified Gemini/OpenAI/DeepSeek/mock provider with retry (3 attempts, exponential backoff), token counting, latency tracking, and API-key-aware client caching. DeepSeek uses OpenAI SDK with custom `base_url`.
- `app/core/llm/models.py` — `LLMResponse` and `LLMChunk` dataclasses
- `Arbiter.evaluate_stream()` — Chunked streaming evaluation with `[UNC]` rollback insertion and max-rollback halting
- Pipeline sets `model_id` and `token_count` on every trace
- Auto-selection priority: Gemini → OpenAI → DeepSeek → mock
- Mock fallback when no API keys are configured

---

## Phase 3: DB-Backed LLM Critics ✅ COMPLETE

**What was built:**
- `Arbiter.load_from_registry(db_session)` — Reads active nodes from `critic_registry`, instantiates with DB-configured prompt templates and thresholds
- `LLMReasoningCritic` / `LLMInjectionCritic` in `nodes.py` — Call LLM provider with prompt template; heuristic pre-filters as fast path
- `app/services/replay.py` — Critic re-evaluation service comparing new scores vs originals
- `POST /api/traces/{id}/re-evaluate` endpoint for drift detection

---

## Phase 4: Full Governance ✅ COMPLETE

**What was built:**
- `app/core/covernor/token_manager.py` — ECDSA key pair generation, token signing/verification with scope, expiry, and single-use enforcement
- K-of-N approval workflow — `ApprovalRequest` creation on `require_approval`, quorum vote counting, auto-deny on expiry or deny vote
- `app/services/integrity.py` — Hash-chain computation (`trace_hash`, `prev_hash`) on every trace
- `GET /api/traces/session/{session_id}/verify-chain` — Tamper-evident chain verification endpoint

---

## Phase 5: A-S-FLC Integration ✅ COMPLETE

**What was built:**
- `app/core/asflc/analyzer.py` — LLM-powered path decomposition with structured JSON parsing into `DecisionPath` objects
- Pipeline step 2: A-S-FLC analysis between immune scan and LLM generation; produces system hint for guiding generation
- Trace fields populated: `asflc_result`, `asflc_chosen_path`, `asflc_confidence`, `asflc_loops`
- `scripts/import_asflc_dataset.py` — CLI script to import the 806-example HuggingFace dataset as golden labeling items

---

## Phase 6: Training Flywheel ✅ COMPLETE

**What was built:**
- `app/services/doctrine_bridge.py` — HTTP client for Doctrine Lab (`POST /api/datasets/import`, `POST /api/eval/report`, `POST /api/finetune/openai/start`); idempotent via `batch_id`
- `app/core/training/calibration.py` — ECE (Expected Calibration Error) tracker with rolling window, per-node breakdown, recalibration flagging
- `app/core/training/evidential.py` — Uncertainty metadata enrichment (A-S-FLC confidence/regret, critic scores, ECE adjustment) for training exports
- `app/core/training/scheduler.py` — Background daemon thread for periodic labeling queue export with optional Doctrine Lab push
- `app/api/training.py` — 7 endpoints: labeling queue, label items, export, eval, finetune, calibration report, LoRA compare
- LoRA hot-swap comparison: temporarily swaps adapter path, re-evaluates test traces, restores original (with `try/finally` safety)

---

## Phase 7: Agent-Immune Upgrade ✅ COMPLETE

**What was built:**
- 11-language injection patterns (English, Spanish, French, German, Portuguese, Chinese, Japanese, Korean, Russian, Arabic, Hindi) with language-specific escalation phrases
- `MemoryBank` — Token-set Jaccard similarity matching for known attack signatures; auto-learns from blocked attacks; bounded at 10K signatures with LRU eviction
- `PromptHardener` — Strips detected injection fragments from flagged (non-blocked) prompts before LLM generation
- `EscalationTracker` — Per-session cumulative threat score with time-decay; auto-blocks sessions exceeding threshold; bounded with stale session eviction
- Pipeline wired: `scan_input(prompt, session_id=...)` feeds escalation tracking; `harden_prompt()` applied on FLAG verdicts

---

## Phase 8: Agentic Loop + Tools + CLI + Telegram ✅ COMPLETE

**What was built:**
- `app/agent/agent_loop.py` — ReAct-style agent loop with per-step Covernor gating, reflection, critic evaluation, and task-level reward scoring
- `app/core/agent/types.py`, `registry.py`, `builtin.py` — Pluggable tool system with 5 built-in tools (`shell_exec`, `file_read`, `file_write`, `web_fetch`, `search`), workspace scoping, output truncation, and SSRF protection
- `app/models/step_trace.py` — Granular per-step audit log (action, tool, args, result, governance decision, reflection)
- `app/api/agent.py` — `POST /api/agent/agent/run`, `/resume`, `/feedback` endpoints
- `app/cli.py` — `nexus chat`, `run`, `status`, `approve`, `resume`, `feedback`, `skills` CLI commands
- `app/channels/telegram_bot.py` — Long-polling Telegram bot adapter
- Agent tool governance policies seeded on startup
- `LOCAL_ONLY` mode: blocks all outbound HTTP, remaps cloud model IDs to Ollama
- Ollama as first-class LLM provider with streaming support

---

## Phase 9: Reward-Scored Memory + Skill Generation ✅ COMPLETE

**What was built:**
- `app/models/episode.py` — Episodic memory storing task summaries, tool sequences, outcomes, reward scores, trajectories
- `app/models/skill.py` — Auto-generated reusable workflow templates with immune scanning, reward tracking, and auto-disable
- `app/core/agent/skills.py` — Skill generation from high-reward episodes, Covernor-gated execution, reward decline detection
- `app/api/skills.py` — CRUD + execute API: `GET/POST/PATCH/DELETE /api/skills`, `POST /api/skills/{id}/execute`
- Agent loop skill recall: `_retrieve_skills()` injects matching skill step sequences into the LLM system prompt
- Agent loop episode recall: `_retrieve_episodes()` injects past experience (successes/failures) as context
- `export_agent_trajectories()` in the labeler — DPO-format chosen/rejected pairs from reward-scored episodes

---

## Phase 10: Ollama + LOCAL_ONLY ✅ COMPLETE

**What was built:**
- `LOCAL_ONLY=true` environment variable blocks all outbound network calls
- LLM routes auto-remap cloud model IDs to `ollama:<default_model>` in both `generate()` and `generate_stream()`
- `web_fetch` and `search` tools return errors under LOCAL_ONLY
- Doctrine Lab bridge skips import calls under LOCAL_ONLY
- Dashboard shows `LOCAL` badge when running in local-only mode
- SSRF protection: `web_fetch` manually follows redirects, blocks internal/localhost/metadata IPs at every hop

---

## Phase 11: MCP Governance Proxy + ClawHub Skill Import ✅ COMPLETE

**What was built:**
- **MCP Governance Proxy** (`app/core/mcp/`): FastMCP-based proxy that forwards tool calls to registered MCP backends through Nexus's zero-trust pipeline. Each call runs immune scan → Covernor policy check (namespaced `mcp:{backend}:{tool}`) → forward → hash-chained trace.
- **Backend registry** (`app/core/mcp/config.py`): JSON file-based registry (`mcp_backends.json`) with dataclass model, supporting `streamable_http`, `sse`, and `stdio` transports.
- **Governed tools** (`app/core/mcp/proxy.py`): `GovernedMcpTool` wraps each remote tool with immune scanning + Covernor evaluation. `require_approval` returns JSON-RPC error `-32001`. `LOCAL_ONLY` blocks all MCP connections.
- **HTTP + stdio entrypoints** (`app/core/mcp/server.py`): Streamable HTTP mounted at `/mcp` when `MCP_ENABLED=true`, stdio via `nexus mcp serve`.
- **Backend CRUD API** (`app/api/mcp.py`): `GET/POST/PATCH/DELETE /api/mcp/backends`, `GET /api/mcp/backends/{name}/tools` with governance annotations.
- **Trace columns**: `mcp_backend`, `mcp_tool_name` on `Trace` model for MCP audit queries. `MCP_AUDIT_ALL` controls tracing scope.
- **ClawHub skill import** (`app/core/agent/clawhub_import.py`, `clawhub_convert.py`): Parses SKILL.md (YAML frontmatter + Markdown body), immune-scans content, heuristically converts to Nexus steps (`tool_call`, `instruction`), deduplicates by step hash, persists with `source`, `requirements`, and `raw_source` columns.
- **`instruction` step type**: `execute_skill()` skips instruction steps but they are injected as LLM context during skill recall.
- **Import API**: `POST /api/skills/import` (file upload + URL), `GET /api/skills/{id}/source` (raw SKILL.md). Dashboard import form on skills page. `nexus skills import <path|url>` CLI.
- **Default-deny MCP policy**: `_seed_mcp_policies()` auto-seeds `mcp-default-deny` covering `mcp:*` actions.
- **`LOCAL_ONLY` guards**: MCP proxy refuses backends, HTTP routes return 503, URL skill import blocked.
- **20 new tests** across 3 test files (`test_mcp_proxy.py`, `test_clawhub_import.py`, `test_mcp_cli.py`).

---

## External Resources

### HuggingFace Datasets
- **a-s-flc-decisions**: 806 structured A-S-FLC reasoning examples (finance, security, travel)
  - Use for fine-tuning the decision analysis capability
  - Import via Phase 5.3

### Models
- **Nexus Spin v5.3**: Lightweight causal transformer with memory loops and bilingual Khmer support
  - Integrated as local model option via `model_id: local:repo/name` or `nexus-spin-v5.3`
  - Cached model/tokenizer loading, stub fallback when `transformers`/`torch` not installed
  - Use Unsloth + LoRA for fine-tuning

### Supported LLM Providers (Phase 2+)
- **Gemini** (google-genai): Primary provider, `GEMINI_API_KEY` in .env
- **OpenAI** (openai): Fine-tuning target, `OPENAI_API_KEY` in .env
- **DeepSeek** (OpenAI-compatible): Optional, via custom base_url
- **Local models** (Unsloth/HuggingFace): For inference on fine-tuned LoRA adapters

---

## Database Schema Reference

### traces
The central audit log. Every pipeline run creates exactly one trace.
| Column | Type | Purpose |
|--------|------|---------|
| id | String PK | Unique trace identifier |
| session_id | String (indexed) | Groups multi-turn conversations |
| sequence | Integer | Turn number within session |
| prompt | Text | Original user prompt |
| prompt_hash | String(64) | SHA-256 of prompt |
| immune_verdict | String(20) | "pass", "block", "flag" |
| immune_score | Float | 0.0-1.0 threat score |
| immune_details | JSON | Full scanner output |
| asflc_result | JSON | Full A-S-FLC decision output |
| asflc_chosen_path | String | Name of chosen decision path |
| asflc_confidence | Float | Convergence confidence |
| asflc_loops | Integer | Number of evaluation loops |
| critic_verdict | String(20) | "pass", "rollback", "halt" |
| critic_scores | JSON | Per-node scores |
| critic_rollback_count | Integer | Times rolled back |
| governance_status | String(20) | "approved", "pending", "denied", "auto" |
| governance_policy_id | String | Which policy matched |
| governance_token_id | String | ECDSA token if issued |
| response | Text | Final model response |
| response_hash | String(64) | SHA-256 of response |
| output_scan_verdict | String(20) | Output immune scan result |
| model_id | String | Which model generated the response |
| latency_ms | Float | End-to-end pipeline time |
| token_count | Integer | Tokens used |
| error | Text | Error message if failed |
| status | String(20) | "completed", "blocked", "halted", "pending_approval" |
| prev_hash | String(64) | Hash chain link to previous trace |
| trace_hash | String(64) | SHA-256 of this trace's content |
| mcp_backend | String(120) | MCP backend name (Phase 11, nullable, indexed) |
| mcp_tool_name | String(200) | MCP tool name (Phase 11, nullable, indexed) |
| created_at | DateTime | UTC timestamp |

### critic_registry
Hot-swappable critic node configurations.
| Column | Type | Purpose |
|--------|------|---------|
| id | String PK | |
| name | String (unique) | "reasoning", "injection", "safety", "quality" |
| node_type | String(30) | "arbiter", "safety", "reasoning", "injection", "quality" |
| prompt_template | Text | The LLM evaluation prompt (hot-swappable) |
| prompt_version | Integer | Auto-incremented on template change |
| lora_adapter_path | String | Path to LoRA adapter for this node |
| weight | Float | Scoring weight in Arbiter aggregation |
| threshold_pass | Float | Score >= this → "pass" |
| threshold_halt | Float | Score < this → "fail" |
| can_halt | Boolean | If True, a "fail" stops generation immediately |
| is_active | Boolean | Can be disabled without deleting |

### policies
Covernor governance rules. Default-deny — unknown actions are always blocked.
| Column | Type | Purpose |
|--------|------|---------|
| id | String PK | |
| name | String (unique) | Human-readable policy name |
| action_pattern | String | Glob pattern matching action types |
| resource_pattern | String | Optional resource scope |
| decision | String(20) | "allow", "require_approval", "deny" |
| risk_level | String(20) | "low", "medium", "high", "critical" |
| required_approvals | String | Number of approvals needed |
| priority | String | Lower = evaluated first |

### labeling_queue
Failure traces awaiting human review for the training flywheel.
| Column | Type | Purpose |
|--------|------|---------|
| id | String PK | |
| trace_id | String | Links back to the execution trace |
| source_node | String | Which critic node flagged this |
| failure_type | String | "reasoning", "injection", "safety", "quality", "hallucination" |
| prompt | Text | The original prompt |
| response | Text | The flagged response |
| critic_output | JSON | Full critic evaluation |
| label | String(20) | Human review: "correct_flag", "false_positive", "needs_edit" |
| corrected_response | Text | Human-corrected version |
| status | String(20) | "pending" → "labeled" → "exported" → "trained" |
| training_batch_id | String | Links to training run |

---

## API Reference (40 endpoints)

> **Versioning**: All `/api/*` endpoints below are also available under `/v1/*` (canonical). The `/api/*` paths remain as backward-compatible aliases. Non-API routes (`/health`, `/dashboard`, `/metrics`, `/mcp`) are unversioned.

### Health & Observability (`app/main.py`)
- `GET /health` — Liveness check (app name, version)
- `GET /health/ready` — Readiness check (DB connectivity, uptime)
- `GET /metrics` — Prometheus metrics (pipeline latency, run counts, LLM errors, critic scores, queue depth)

### Agent (`app/api/agent.py`)
- `POST /api/agent/run` — Execute full pipeline. Body: `{"prompt": "...", "session_id": "optional", "model_id": "optional"}`
- `POST /api/agent/stream` — Stream tokens via SSE. Same body as `/run`.
- `POST /api/agent/compare` — Multi-model evaluation. Sends prompt to multiple providers in parallel, critic-scores each response, returns the best. Body: `{"prompt": "...", "model_ids": ["gemini-2.5-flash", "gpt-4o-mini"], "session_id": "optional"}`. Omit `model_ids` to use all configured providers.
- `POST /api/agent/agent/run` — Agentic loop (tools + reflection + reward scoring). Body: `{"prompt": "...", "session_id": "optional", "model_id": "optional", "max_steps": 25}`
- `POST /api/agent/agent/resume` — Resume agent after human approval. Body: `{"trace_id": "..."}`
- `POST /api/agent/agent/feedback` — Attach good/bad feedback to a trace. Body: `{"trace_id": "...", "feedback": "good|bad"}`
- `POST /api/agent/benchmark` — Run security benchmark against the immune scanner. Body: `{"categories": ["encoding_evasion"], "threshold": 0.95}`. Returns per-category detection rates and composite score with optional gate pass/fail.
- `GET /api/agent/cache/stats` — LLM response cache statistics (hits, misses, size, hit rate, TTL).
- `DELETE /api/agent/cache` — Clear the LLM response cache. Returns `{"cleared": N}`.
- `GET /api/agent/circuit-breakers` — Per-provider circuit breaker status (state, recent failures, thresholds).
- `GET /api/agent/tracing` — OpenTelemetry tracing status (enabled, available, service name, exporter endpoint, sample rate).

### Health (`app/main.py`)

- `GET /health` — Liveness probe. Always returns 200 with `{"status": "ok"}`.
- `GET /health/ready` — Readiness probe. Returns 200 (ready) or 503 (degraded). Checks: database connectivity, LLM provider count, circuit breaker states, LLM cache status, OTel tracing, webhooks/MCP enabled, uptime.

### Webhooks (`app/api/webhooks.py`)

- `POST /api/webhooks` — Create webhook. Body: `{"url": "...", "events": ["critic_halt", "input_blocked"], "secret": "...", "description": "..."}`. Events: `approval_needed`, `critic_halt`, `circuit_open`, `input_blocked`, `output_blocked`, `export_complete`, `*` (all).
- `GET /api/webhooks` — List all webhooks.
- `GET /api/webhooks/{id}` — Get webhook details.
- `PATCH /api/webhooks/{id}` — Update webhook (url, events, secret, enabled). Re-enabling resets failure count.
- `DELETE /api/webhooks/{id}` — Delete webhook.
- `POST /api/webhooks/{id}/test` — Send a test event to the webhook.
- `GET /api/webhooks/events/list` — List valid event types.

### Skills (`app/api/skills.py`)
- `GET /api/skills` — List skills with reward stats. Query: `?enabled_only=true`
- `GET /api/skills/{id}` — Full skill detail (steps, reward stats, hash, source episode)
- `POST /api/skills/{id}/execute` — Execute a skill step-by-step with Covernor gating
- `PATCH /api/skills/{id}` — Enable or disable a skill. Body: `{"enabled": true}`
- `DELETE /api/skills/{id}` — Permanently delete a skill

### Traces (`app/api/traces.py`)
- `GET /api/traces` — List traces. Query: `?session_id=&status=&limit=50&offset=0`
- `GET /api/traces/{trace_id}` — Full trace detail
- `GET /api/traces/{trace_id}/replay` — Step-by-step pipeline replay
- `POST /api/traces/{trace_id}/re-evaluate` — Re-run critic tree on stored trace, compare scores for drift detection
- `GET /api/traces/session/{session_id}/verify-chain` — Verify tamper-evident hash chain integrity

### Critic (`app/api/critic.py`)
- `GET /api/critic/registry` — List critic nodes. Query: `?node_type=&active_only=true`
- `POST /api/critic/registry` — Register a new node
- `PATCH /api/critic/registry/{node_id}` — Update node config (hot-swap prompts, LoRA paths, thresholds)

### Governance (`app/api/governance.py`)
- `GET /api/governance/policies` — List policies. Query: `?active_only=true`
- `POST /api/governance/policies` — Create policy
- `GET /api/governance/approvals` — List approval requests. Query: `?status=pending`
- `POST /api/governance/approve/{request_id}` — Submit vote. Body: `{"approver_id": "...", "decision": "approve|deny", "reason": "optional"}`
- `GET /api/governance/training/queue` — View labeling queue (alias of `GET /api/training/queue`; kept for backward compatibility)

### Training (`app/api/training.py`)
- `GET /api/training/queue` — View labeling queue. Query: `?status=&failure_type=`
- `POST /api/training/queue/{item_id}/label` — Apply human label to queued item
- `POST /api/training/export` — Export labeled items (optionally push to Doctrine Lab)
- `POST /api/training/eval` — Submit evaluation report to Doctrine Lab
- `POST /api/training/finetune` — Trigger fine-tuning job via Doctrine Lab
- `GET /api/training/finetune/status/{job_id}` — Poll fine-tune job status from Doctrine Lab
- `POST /api/training/promote-adapter` — Promote completed LoRA adapter to a critic node. Body: `{"job_id": "...", "node_name": "..."}`
- `GET /api/training/calibration` — ECE calibration report. Query: `?node_name=`
- `POST /api/training/calibration/persist` — Persist current in-memory ECE metrics to DB
- `GET /api/training/calibration/snapshots` — List persisted ECE snapshots. Query: `?limit=20`
- `POST /api/training/lora/compare` — Compare critic before/after LoRA adapter swap

### MCP Governance Proxy (Phase 11)
- `GET /api/mcp/backends` — List configured MCP backends
- `POST /api/mcp/backends` — Register a new MCP backend. Body: `{"name": "...", "url": "...", "transport": "streamable_http"}`
- `PATCH /api/mcp/backends/{name}` — Update a backend's URL, transport, or enabled status
- `DELETE /api/mcp/backends/{name}` — Remove a backend
- `GET /api/mcp/backends/{name}/tools` — List tools from a backend with governance annotations (policy status per tool)

### Skill Import (Phase 11)
- `POST /api/skills/import` — Import a SKILL.md (file upload via `file` or URL via `url` form field). Returns `{"skill_id": "..."}`
- `GET /api/skills/{id}/source` — Raw SKILL.md content for an imported skill

---

## Testing — 971+ tests across 43 files

- All tests in `tests/` directory
- Fixtures in `tests/conftest.py` (test DB, session, TestClient)
- Test files named `test_<module>.py`
- Each core module has its own test file
- API tests use `TestClient` via the `client` fixture
- DB tests use the `db_session` fixture (auto-rollback)
- Mock external APIs — never make real LLM calls in tests
- Run: `pytest tests/ -v`

| Test File | Coverage |
|-----------|----------|
| `test_pipeline.py` | Full pipeline orchestration, API endpoints, hot-swap |
| `test_immune.py` | 11-language scanner, memory bank, escalation, hardener |
| `test_critic.py` | All 4 critic nodes, Arbiter, DB-backed loading |
| `test_covernor.py` | Policy engine, ECDSA tokens, K-of-N approval |
| `test_asflc.py` | A-S-FLC engine, convergence, chain regret |
| `test_analyzer.py` | LLM path decomposition (mocked) |
| `test_llm_provider.py` | Gemini/OpenAI/DeepSeek/mock providers, retry logic, routing |
| `test_streaming_critic.py` | Streaming evaluation, [UNC] insertion, max rollbacks |
| `test_re_evaluate.py` | Critic replay, drift detection, 404 handling |
| `test_governance.py` | Full approval workflow, expiry, hash chain verification |
| `test_integrity.py` | Hash-chain computation and tamper detection |
| `test_training.py` | Doctrine Lab bridge, labeling flow, export, training API, finetune status, adapter promotion, calibration |
| `test_advanced_training.py` | ECE calibration, evidential enrichment, scheduler |
| `test_middleware.py` | API key auth, rate limiting, dashboard auth (login/logout/session), key rotation (multi-key parsing, primary/secondary distinction, deprecation header, dashboard multi-key login), request body size limit (oversized rejection, disabled-when-zero, GET passthrough), security headers (baseline headers, Permissions-Policy, API vs dashboard CSP, script-src none, form-action self, HSTS in prod only, headers on errors and health endpoints), legacy /api/ deprecation (Deprecation header on legacy only, Link to /v1/ successor, Sunset when configured, no headers on /v1//health/dashboard, nested path mapping) |
| `test_dashboard_csrf.py` | CSRF token validation for dashboard POST forms |
| `test_e2e.py` | Full pipeline lifecycle, hash chain, labeling+export, error atomicity, critic halt, approvals, metrics |
| `test_agent_loop.py` | Agent loop execution, step traces, episodes, max steps, critic halt, API endpoints, trajectory export, tool security |
| `test_skills.py` | Skill generation, execution, reward decline, API CRUD, skill recall in agent loop |
| `test_retention.py` | Data retention purge (traces, labeling, approvals, calibration) |
| `test_stream_endpoint.py` | SSE streaming happy path, blocked input, critic halt, output scan, governance, errors |
| `test_compare.py` | Multi-model compare endpoint |
| `test_mcp_proxy.py` | MCP proxy governance, namespaced policies, LOCAL_ONLY, backend CRUD, tool forwarding |
| `test_clawhub_import.py` | SKILL.md parsing, step conversion, instruction steps, immune blocking, dedup, raw_source |
| `test_mcp_cli.py` | CLI subcommands: nexus mcp serve/backends/add, nexus skills import |
| `test_redteam.py` | Adversarial red-team (145 tests, 15 categories): structural injection, encoding evasion, multi-language advanced, indirect/contextual, compound/chained, output scan evasion, hardener edge cases, memory bank adversarial, false-positive resilience, scoring thresholds, escalation tracker lifecycle, output scan edge cases, language pattern completeness, non-English encoding tricks, pipeline integration |
| `test_benchmark.py` | Security benchmark: runner, per-category scoring, API endpoint, CLI commands, attack registry integrity, CI gating |
| `test_circuit_breaker.py` | Circuit breaker state machine, rolling window, half-open recovery, provider fallback chain, concurrent access, stream fallback |
| `test_llm_cache.py` | LLM response cache: hit/miss, TTL expiry, LRU eviction, invalidation, stats, concurrency, provider integration, security invariants (governance not bypassed), API endpoints |
| `test_webhooks.py` | Webhook system: HMAC signing/verification, delivery with retries, event filtering, wildcard subscription, disabled skip, API CRUD, pipeline integration (input_blocked fires webhook), exponential backoff (jitter bounds, cap, variation, settings defaults, zero base), retryable status classification (5xx/408/429 retryable, 4xx non-retryable), backoff integration (connection error retries with sleep count, non-retryable 4xx immediate failure, retryable 5xx retry, success-on-second-attempt, single-retry config), config validation (negative backoff base, max < base, zero retries, zero timeout, zero max failures) |
| `test_tracing.py` | OpenTelemetry tracing: no-op fallback, init/shutdown lifecycle, real span creation, exception recording, nested span context propagation, pipeline span integration, log-trace correlation (JSON + text formatters, active span context), health check probes (full shape, DB down), API endpoint |
| `test_rate_limiter.py` | Rate limiter backends: in-process (allow/block, window expiry, eviction, reset, check returns RateLimitResult with remaining, blocked has retry_after, remaining decreases), Redis mocked (Lua script check under/over limit, is_allowed legacy compat, error fail-open with failed reconnect, client-none tries reconnect, reconnect cooldown), backend singleton, Redis fallback, health status, X-Trace-ID header |
| `test_error_responses.py` | Structured error envelope shape, NexusAPIError attributes, API error format (400/401/404/409/413/422/429/500/503/504), shutdown guard, MCP LOCAL_ONLY guard, body size limit, v1/legacy envelope consistency, status code map coverage, ISO timestamp, backward-compatible `detail` field |
| `test_graceful_shutdown.py` | Shutdown coordinator: initial state, request tracking (increment/decrement/nested/exception), drain mode, async drain wait (immediate, timeout, completion), reset, singleton; middleware: normal pass-through, 503 during drain (v1 + legacy + traces + critic), health/readiness/static/docs/openapi exempt; concurrency: 10-thread parallel tracking, multi-request drain wait, in-flight completion after drain starts, drain timeout with stuck requests, exception safety under concurrency |
| `test_config_validator.py` | Startup validation: security (prod API key/session secret), contradictions (MCP+LOCAL_ONLY, OTel+LOCAL_ONLY, webhooks+LOCAL_ONLY), numeric bounds (sample rate, RPM, prompt length, quorum, CB threshold, agent steps), timeout coherence (request vs drain vs gunicorn), LLM providers (none, one, local-only), database (SQLite in prod), multi-worker (Redis, scheduler skip) |
| `test_api_versioning.py` | API versioning: /v1/* canonical routes, /api/* legacy backward-compat, parity between both prefixes, rate limiting on both, unversioned routes (health, dashboard), v1 404 for non-API paths |
| `test_alembic_migrations.py` | Migration smoke: upgrade to head, downgrade -1, full downgrade to base, upgrade/downgrade/upgrade round-trip, stepwise upgrade of each revision, stepwise downgrade of each revision, single-head chain integrity, every revision has downgrade(), model-migration drift detection |
| `test_request_id_propagation.py` | Request ID propagation: correlation headers builder, mock response carries request_id, pipeline end-to-end propagation, OpenAI/DeepSeek extra_headers injection, no headers when no request context, LLMResponse dataclass field |
| `test_cors.py` | CORS hardening: config validator warns on wildcard/schemeless origins in prod, negative max_age, middleware reflects allowed origins, rejects disallowed origins, preflight method/header/max-age, credential safety with wildcard, config parsing and defaults |
| `test_db_pool.py` | Pool config: _build_engine_kwargs for SQLite vs PostgreSQL, pool_recycle zero/nonzero, config validation (pool_size, max_overflow, recycle, timeout bounds), readiness endpoint pool status, default settings |
| `test_sanitization.py` | Input sanitization: sanitize_for_log (newline/tab escape, control char strip, truncation, log injection attack), sanitize_for_error (quoting, newline replacement, control char strip, truncation, unicode), API integration (critic/governance 409 name sanitization, newline/control-char stripping in error messages, MCP 502 no exception leak) |
| `test_prometheus_middleware.py` | Prometheus HTTP metrics: path normalization (v1/api ID collapse, non-API passthrough, deep nesting), DB pool gauge updates (pooled engine, noop without prometheus, silent on exception), metric type assertions (Histogram, Counter, Gauge), middleware integration (latency recorded, status class labels, method labels, in-flight gauge, DB pool gauges, path ID collapse, POST counting, 2xx/4xx counting, accumulation) |
| `test_health_probe.py` | Deep health check: individual provider probes (Gemini reachable/unreachable, OpenAI reachable/auth failure, DeepSeek reachable/timeout, Ollama reachable/refused), concurrent probe_providers (no providers, single, multiple, settings timeout, mixed reachable/unreachable), readiness endpoint integration (shallow no probes, deep empty, deep with provider, unreachable warning, deep=false same as shallow, unreachable does not degrade status), config validation (zero/negative probe timeout) |
| `test_audit_export.py` | SIEM audit export: trace-to-record mapping (pipeline_run, input_blocked, output_blocked, critic_halt, governance_denied, MCP fields, error inclusion), approval-to-record mapping (pending→requested, resolved, policy_id), export service (empty DB, filter by event type/time range/status, limit, approvals, timestamp sorting, max limit cap), JSONL serialization (empty, single, multiple, trailing newline, compact), event types listing, API integration (JSONL format/headers, JSON format, event types endpoint, event_type filter param, legacy route, pagination, valid JSONL lines) |
| `test_idempotency.py` | Idempotency key support: InProcessStore (set/get, missing key, TTL expiration, LRU eviction, reset, backend type, move-to-end on get), RedisStore mocked (set/get, get-none, get-error, set-error, backend type connected/disconnected, connected property, reset scan+delete, None client), get_store singleton (default in-process, singleton pattern, reset clears), middleware integration (no key processes normally, first request caches + replay, different keys different responses, key too short/long rejected, GET ignores key, non-idempotent path ignores key, legacy /api/ path works), in-flight dedup InProcessStore (acquire/release, double-acquire fails, release allows reacquire, independent keys, release nonexistent safe, reset clears inflight), in-flight dedup RedisStore (SET NX call, NX fail returns false, release calls delete, None client safe, error falls through), middleware in-flight (concurrent duplicate returns 409, inflight released after success, cached response skips inflight), config validation (low TTL warning, zero max keys error, defaults no issues) |
| `test_circuit_breaker_dashboard.py` | Circuit breaker dashboard: enriched status (since_last_failure none/populated, recovery_remaining none/present, rolling_window, half_open_successes), dashboard HTML (empty page, shows provider, open provider with reset button, stats counts, reset button absent for closed, nav link), reset endpoint (reset open provider, unknown 404, redirect), JSON API enriched fields (rolling_window, since_last_failure, half_open_successes, recovery_remaining when open) |
| `test_provider_health.py` | Unified provider health: _compute_overall logic (unconfigured, healthy no signals, healthy with closed CB, down when open, degraded when half-open, degraded when probe unreachable, healthy when reachable, down overrides probe), get_provider_health service (returns all providers, configured flag, CB state included/none, probe results when run/not run, overall status, display name), dashboard HTML (page loads, shows names, unconfigured badge, probe button, probed badge, nav link, CB state for open provider, reset redirect), JSON API (all providers, overall_status field, probe false by default, probe true runs probes, legacy route) |
| `test_api_versioning.py` | API versioning: version module (semver format, required keys, API_VERSION matches, python_version present, git_sha type, git_sha when subprocess fails), X-API-Version header (health, readiness, API endpoint, dashboard, 404), Accept-Version mismatch (no header, matching, different triggers mismatch, empty string ignored), /v1/agent/version endpoint (returns 200, required fields, legacy route) |

---

## Commands

```bash
# Development
source venv/bin/activate
uvicorn app.main:app --reload --port 9000

# Tests
pytest tests/ -v --tb=short

# Lint & format
ruff check app/ tests/
ruff format --check app/ tests/

# Type checking (strict)
mypy app/

# Dependency audit
pip-audit -r requirements.txt

# New migration after model changes
alembic revision --autogenerate -m "description"
alembic upgrade head

# Check existing tables
alembic current

# Security benchmark
nexus benchmark                           # table output
nexus benchmark --json                    # JSON for CI
nexus benchmark --threshold 0.95          # fail if score < 95%
nexus benchmark --categories encoding_evasion,multilingual

# Docker (SQLite, default)
docker compose up --build

# Docker (PostgreSQL)
docker compose --profile postgres up --build
```
