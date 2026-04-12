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

### Core Frameworks Being Integrated

| Framework | Role | Status |
|-----------|------|--------|
| **Agent-Immune** | Adaptive threat intelligence, semantic memory, 11-language injection detection | Phase 1 done (rule-based), Phase 7 full integration |
| **A-S-FLC** | Asymmetric Signed Force-Loop-Chain — risk-penalized decision engine | Engine complete, needs pipeline wiring (Phase 5) |
| **Covernor** | Default-deny policy engine, ECDSA tokens, K-of-N approval | Phase 1 done (HMAC), Phase 4 full ECDSA |
| **GrokForge-Nexus v16** | Arbiter critic tree, chunked generate-then-verify, auto-rollback | Phase 1 done (post-hoc), Phase 2 streaming |
| **Nexus Spin v5.3** | Lightweight causal transformer with memory loops, bilingual Khmer support | Phase 6 integration |
| **a-s-flc-decisions** | 806 structured A-S-FLC reasoning examples on HuggingFace | Phase 6 fine-tuning |

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
- `app/core/immune/scanner.py` — Input injection detection (9 regex patterns) + output leak scanning (3 patterns) + escalation phrases
- `app/core/asflc/engine.py` — Full A-S-FLC with EventNode, DecisionPath, convergence loop, chain regret calculation
- `app/core/covernor/policy_engine.py` — Default-deny with glob pattern matching
- `app/core/covernor/token_manager.py` — HMAC single-use capability tokens
- `app/core/critic/arbiter.py` — Central Arbiter with CriticNodeProtocol, register/unregister, max rollback enforcement
- `app/core/critic/nodes.py` — ReasoningCritic, InjectionCritic (can_halt), SafetyCritic (can_halt), QualityCritic
- `app/core/training/labeler.py` — push_failure, label_item, get_queue, export_for_training

### Pipeline & API
- `app/agent/pipeline.py` — Full 6-step orchestration (mock LLM)
- 13 API endpoints across 4 routers
- Alembic migration generated and tested
- 4 default governance policies seeded on startup

### Tests
- 56 tests across 5 files, all passing
- Coverage: immune scanner, A-S-FLC engine, all 4 critic nodes, Arbiter, policy engine, token manager, full pipeline, all API endpoints

---

## Phase 2: Live Critic Layer

**Goal:** Replace the mock LLM with real provider calls and implement chunked streaming evaluation.

### Tasks

#### 2.1 LLM Provider Service
Create `app/core/llm/provider.py`:
- Unified interface for calling Gemini, OpenAI, and local models
- Streaming support (async generator yielding chunks)
- Token counting and latency tracking
- Automatic retry with exponential backoff
- Model selection from config or per-request override

```python
class LLMProvider:
    async def generate(self, system: str, prompt: str, stream: bool = False) -> LLMResponse: ...
    async def generate_stream(self, system: str, prompt: str) -> AsyncGenerator[str, None]: ...
```

#### 2.2 Replace Mock in Pipeline
In `app/agent/pipeline.py`:
- Replace `_mock_generate()` with real `LLMProvider.generate()` call
- Add `model_id` to trace record
- Add `token_count` tracking
- Keep mock as fallback when no API key is configured

#### 2.3 Chunked Generate-Then-Verify (Streaming)
Upgrade `app/core/critic/arbiter.py`:
- Add `evaluate_stream(chunks)` method
- Accumulate chunks up to `CRITIC_CHUNK_SIZE` tokens
- Run all leaf nodes on each accumulated chunk
- On rollback: insert `[UNC]` token, regenerate from last safe checkpoint
- On halt: stop generation, push to labeling queue
- Track chunk-level scores in trace

#### 2.4 Tests
- Test real LLM calls with mocked API responses
- Test streaming evaluation with multi-chunk input
- Test rollback + [UNC] insertion
- Test fallback to mock when no API key

### Stop Condition
After Phase 2: The pipeline must make real LLM calls (at least Gemini), the `/traces` endpoint must show actual model responses with token counts, and the chunked evaluation must work end-to-end. Show test results before proceeding.

---

## Phase 3: LLM-Backed Leaf Nodes

**Goal:** Upgrade critic nodes from heuristic-based to LLM-backed evaluation with hot-swappable prompts.

### Tasks

#### 3.1 DB-Driven Critic Loading
In `app/core/critic/arbiter.py`:
- Add `load_from_registry(db_session)` method
- Read active nodes from `critic_registry` table
- Instantiate nodes with their DB-configured prompt templates and thresholds
- Support runtime reload without restart

#### 3.2 LLM-Backed ReasoningCritic
Upgrade `app/core/critic/nodes.py`:
- Call LLM with the node's prompt template from the registry
- Parse structured JSON score response
- Fall back to heuristic if LLM call fails
- Score dimensions: logical_coherence, factual_accuracy, completeness, consistency

#### 3.3 LLM-Backed InjectionCritic
Upgrade to use LLM for detection:
- Prompt template asks model to check for role-breaking, system prompt leaks, and indirect injections
- Return confidence score + specific violation details
- Keep regex patterns as fast pre-filter (skip LLM if regex already catches it)

#### 3.4 Replay Service
Create `app/services/replay.py`:
- Load a trace by ID
- Re-run the critic tree on the stored prompt + response
- Compare new scores vs original scores
- Useful for testing prompt template changes without re-running the full pipeline

#### 3.5 Tests
- Test DB-driven node loading
- Test LLM-backed critic scoring (mocked LLM)
- Test replay service produces consistent results
- Test hot-swap: update prompt template, verify next evaluation uses new template

### Stop Condition
After Phase 3: All 4 critic nodes must load from the DB, the ReasoningCritic and InjectionCritic must use LLM calls, the replay service must work via `/api/traces/{id}/replay`, and all tests pass. Show the full code and test results.

---

## Phase 4: Full Governance Layer

**Goal:** Replace HMAC tokens with real ECDSA, implement the full K-of-N approval workflow.

### Tasks

#### 4.1 ECDSA Token Manager
Upgrade `app/core/covernor/token_manager.py`:
- Generate ECDSA key pair on first run (or load from `ECDSA_PRIVATE_KEY_PATH`)
- Sign capability tokens with the private key
- Verify tokens with the public key
- Tokens include: trace_id, action_type, scope, issued_at, expires_at
- Single-use enforcement (consumed flag + DB record)

#### 4.2 K-of-N Approval Workflow
Enhance `app/api/governance.py`:
- When policy says `require_approval`: create an ApprovalRequest, hold the pipeline
- Approvers submit votes via `POST /api/governance/approve/{request_id}`
- When `received_approvals >= required_approvals`: mint ECDSA token, resume pipeline
- If any vote is "deny": deny the entire request
- Add expiry: if not resolved within TTL, auto-deny

#### 4.3 Approval Console API
Add endpoints:
- `GET /api/governance/approvals/pending` — list actions awaiting approval
- `GET /api/governance/approvals/{id}` — full details including action payload
- `POST /api/governance/approvals/{id}/vote` — submit approve/deny with reason

#### 4.4 Hash Chain Enforcement
Wire the `prev_hash` and `trace_hash` fields in the Trace model:
- Each trace computes SHA-256 of its own content
- `prev_hash` links to the previous trace's `trace_hash`
- Add `GET /api/traces/verify-chain` endpoint to validate integrity

#### 4.5 Tests
- Test ECDSA sign/verify cycle
- Test K-of-N quorum logic (2-of-3, etc.)
- Test expiry auto-deny
- Test hash chain integrity verification

---

## Phase 5: A-S-FLC Pipeline Integration

**Goal:** Wire the decision engine into the live pipeline so every prompt goes through risk-penalized decision analysis.

### Tasks

#### 5.1 LLM-Powered Decision Analysis
Create `app/core/asflc/analyzer.py`:
- Prompt the LLM to decompose the user's request into decision paths
- Each path has events with probability, impact, and is_positive
- Parse the structured JSON into `DecisionPath` objects
- Run through `evaluate_paths()` convergence loop

**System prompt for analysis:**
```
You are a risk-aware decision analyst. Given a user request, identify 2-4 
possible action paths. For each path, list the key events that could happen,
with probability (0-1), impact (-1000 to +1000), and whether the event is 
positive. Return as JSON array of paths.
```

#### 5.2 Pipeline Integration
In `app/agent/pipeline.py`, add Step 2.5 between input scan and generation:
- If the prompt involves an action (tool call, decision, etc.), run A-S-FLC
- If the chosen path's confidence < threshold, flag for human review
- If chain_regret > 0, add warning to trace
- Store full A-S-FLC result in trace fields: `asflc_result`, `asflc_chosen_path`, `asflc_confidence`, `asflc_loops`

#### 5.3 A-S-FLC Fine-Tuning Data
Import the **a-s-flc-decisions** HuggingFace dataset (806 examples):
- Create an import script that loads examples into the labeling queue
- Use these as golden examples for evaluating A-S-FLC output quality
- Export in fine-tuning format for model improvement

#### 5.4 Tests
- Test LLM-powered path decomposition (mocked)
- Test pipeline with A-S-FLC enabled
- Test confidence threshold gating
- Test chain regret warnings

---

## Phase 6: Training Flywheel

**Goal:** Close the loop — failures detected by the critic tree automatically improve the model.

### Tasks

#### 6.1 Automated Export Pipeline
Create `app/core/training/flywheel.py`:
- Scheduled job that checks the labeling queue for reviewed items
- Export labeled items in OpenAI JSONL format
- Optionally trigger fine-tuning via OpenAI API or generate Unsloth script
- Track training batches in a new `training_runs` table

#### 6.2 LoRA Hot-Swap
Implement adapter swapping:
- Store LoRA adapter paths in `critic_registry` table
- When a new adapter is trained, update the path via `PATCH /api/critic/registry/{id}`
- Next critic evaluation uses the new adapter
- Endpoint to compare before/after adapter performance

#### 6.3 ECE Calibration
Implement Expected Calibration Error tracking:
- After each critic evaluation, log predicted confidence vs actual outcome
- Compute ECE over a rolling window
- If ECE exceeds threshold, flag for recalibration
- Store calibration history for monitoring

#### 6.4 Evidential Loss Integration
When exporting training data, add evidential loss metadata:
- Uncertainty estimates from A-S-FLC decisions
- Critic confidence scores
- Calibration adjustments
- This metadata helps the fine-tuning process weight examples appropriately

#### 6.5 Doctrine Lab Bridge
Create `app/core/training/doctrine_bridge.py`:
- Import curated datasets from Doctrine Lab via its export API
- Push Nexus Agent failure traces to Doctrine Lab's evaluation pipeline
- Trigger Doctrine Lab benchmarks to prove improvement after each training round

#### 6.6 Tests
- Test automated export pipeline
- Test LoRA path hot-swap
- Test Doctrine Lab integration (mocked HTTP)
- Test ECE calibration computation

---

## Phase 7: Agent-Immune Full Integration

**Goal:** Replace the rule-based scanner with the full Agent-Immune system.

### Tasks

#### 7.1 Semantic Memory Bank
Implement `app/core/immune/memory.py`:
- Store known attack patterns as embeddings
- Use semantic similarity to catch rephrased attacks that bypass regex
- Learn from new attacks: when a prompt is blocked by the critic tree but passed the scanner, add it to the memory bank
- Start with the 92 known incidents from Agent-Immune research

#### 7.2 Multi-Turn Escalation Tracking
Add session-level tracking:
- Track escalation patterns across multiple turns within a session
- If a user gradually pushes boundaries (each message slightly more aggressive), flag the session
- Use the `session_id` and `sequence` fields in the Trace model

#### 7.3 PromptHardener
Implement `app/core/immune/hardener.py`:
- Role-lock: inject strict role boundaries into the system prompt
- Sandboxing: constrain the model's self-reference capabilities
- Output guards: post-processing rules that strip leaked content

#### 7.4 Multi-Language Support
Extend scanner to handle 11 languages:
- English, Spanish, Chinese, Arabic, French, German, Portuguese, Russian, Japanese, Korean, Hindi
- Translation-invariant detection patterns
- Language-specific escalation phrases

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

## API Reference

### Agent
- `POST /api/agent/run` — Execute full pipeline. Body: `{"prompt": "...", "session_id": "optional", "model_id": "optional"}`

### Traces
- `GET /api/traces` — List traces. Query: `?session_id=&status=&limit=50&offset=0`
- `GET /api/traces/{trace_id}` — Full trace detail
- `GET /api/traces/{trace_id}/replay` — Step-by-step pipeline replay

### Critic
- `GET /api/critic/registry` — List critic nodes. Query: `?node_type=&active_only=true`
- `POST /api/critic/registry` — Register a new node
- `PATCH /api/critic/registry/{node_id}` — Update node config (hot-swap)

### Governance
- `GET /api/governance/policies` — List policies
- `POST /api/governance/policies` — Create policy
- `GET /api/governance/approvals` — List pending approvals
- `POST /api/governance/approve/{request_id}` — Submit vote. Body: `{"approver_id": "...", "decision": "approve|deny", "reason": "optional"}`

### Training
- `GET /api/governance/training/queue` — View labeling queue

---

## Testing Conventions

- All tests in `tests/` directory
- Fixtures in `tests/conftest.py` (test DB, session, TestClient)
- Test files named `test_<module>.py`
- Each core module has its own test file
- API tests use `TestClient` via the `client` fixture
- DB tests use the `db_session` fixture (auto-rollback)
- Mock external APIs — never make real LLM calls in tests
- Run: `pytest tests/ -v`

---

## Commands

```bash
# Development
source venv/bin/activate
uvicorn app.main:app --reload --port 9000

# Tests
pytest tests/ -v --tb=short

# New migration after model changes
alembic revision --autogenerate -m "description"
alembic upgrade head

# Check existing tables
alembic current
```
