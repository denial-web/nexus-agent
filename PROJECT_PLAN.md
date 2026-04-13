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
| **Nexus Spin v5.3** | Lightweight causal transformer with memory loops, bilingual Khmer support | Future — local model integration |
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

## External Resources

### HuggingFace Datasets
- **a-s-flc-decisions**: 806 structured A-S-FLC reasoning examples (finance, security, travel)
  - Use for fine-tuning the decision analysis capability
  - Import via Phase 5.3

### Models
- **Nexus Spin v5.3**: Lightweight causal transformer with memory loops and bilingual Khmer support
  - Integrate as a local model option in Phase 6
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

## API Reference (23 endpoints)

### Health (`app/main.py`)
- `GET /health` — Liveness check (app name, version)
- `GET /health/ready` — Readiness check (DB connectivity, uptime)

### Agent (`app/api/agent.py`)
- `POST /api/agent/run` — Execute full pipeline. Body: `{"prompt": "...", "session_id": "optional", "model_id": "optional"}`

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
- `GET /api/governance/training/queue` — View labeling queue (alias)

### Training (`app/api/training.py`)
- `GET /api/training/queue` — View labeling queue. Query: `?status=&failure_type=`
- `POST /api/training/queue/{item_id}/label` — Apply human label to queued item
- `POST /api/training/export` — Export labeled items (optionally push to Doctrine Lab)
- `POST /api/training/eval` — Submit evaluation report to Doctrine Lab
- `POST /api/training/finetune` — Trigger fine-tuning job via Doctrine Lab
- `GET /api/training/calibration` — ECE calibration report. Query: `?node_name=`
- `POST /api/training/lora/compare` — Compare critic before/after LoRA adapter swap

---

## Testing — 243+ tests across 17 files

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
| `test_training.py` | Doctrine Lab bridge, labeling flow, export, training API |
| `test_advanced_training.py` | ECE calibration, evidential enrichment, scheduler |

---

## Commands

```bash
# Development
source venv/bin/activate
uvicorn app.main:app --reload --port 9000

# Tests
pytest tests/ -v --tb=short

# Lint
ruff check app/ tests/

# New migration after model changes
alembic revision --autogenerate -m "description"
alembic upgrade head

# Check existing tables
alembic current

# Docker (SQLite, default)
docker compose up --build

# Docker (PostgreSQL)
docker compose --profile postgres up --build
```
