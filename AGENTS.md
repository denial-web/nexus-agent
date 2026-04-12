# AGENTS.md — Nexus Agent

## What This Project Is

Nexus Agent is a **Zero-Trust & Self-Evolving AI Agent System**. It wraps LLM calls in strict security boundaries (input scanning, governance approval, output scanning) and uses a critic tree to evaluate every generation. Failures feed back into a labeling queue for continuous fine-tuning.

**Sister project**: [Doctrine Lab](../thinking-DT/doctrine-lab/) provides the dataset generation, curation, fine-tuning pipeline, and benchmark evaluation. Nexus Agent is the runtime; Doctrine Lab is the training factory.

**Doctrine Lab Milestone 1 (B.1 + B.2) is COMPLETE**: Doctrine Lab now has agent-safety eval tasks (`agent_safety`, `agent_reasoning`, `agent_governance`, `injection_resistance`) and a `POST /api/datasets/import` endpoint ready to receive Nexus failure traces.

## Tech Stack

- **Python 3.13**, FastAPI, SQLAlchemy 2.0, Alembic, Pydantic v2
- **Database**: SQLite (dev), PostgreSQL (prod)
- **AI providers**: Google Gemini (`google-genai`), OpenAI (`openai`) — both already in `requirements.txt`
- **Crypto**: `cryptography` (for ECDSA capability tokens)
- **Testing**: pytest + httpx TestClient

## Project Structure

```
app/
├── main.py                 # FastAPI app, lifespan, migration runner, policy seeding
├── config.py               # Pydantic Settings (all configurable via .env)
├── db.py                   # SQLAlchemy engine, session, Base
├── agent/
│   └── pipeline.py         # Full zero-trust pipeline orchestrator (THE CORE)
├── api/
│   ├── agent.py            # POST /api/agent/run
│   ├── traces.py           # GET /api/traces, GET /api/traces/{id}/replay
│   ├── critic.py           # CRUD /api/critic/registry
│   ├── governance.py       # Policies, approvals, labeling queue
│   └── training.py         # Labeling, export, eval, fine-tuning endpoints
├── services/
│   ├── integrity.py        # Hash-chain computation and verification
│   ├── replay.py           # Critic re-evaluation service
│   └── doctrine_bridge.py  # Doctrine Lab HTTP client
├── core/
│   ├── immune/scanner.py   # Multi-lang injection, memory bank, escalation, hardening
│   ├── asflc/
│   │   ├── engine.py       # A-S-FLC decision framework
│   │   └── analyzer.py     # LLM-powered path decomposition
│   ├── llm/
│   │   ├── models.py       # LLMResponse, LLMChunk dataclasses
│   │   └── provider.py     # Gemini/OpenAI/mock LLM provider
│   ├── covernor/
│   │   ├── policy_engine.py    # Default-deny policy evaluation
│   │   └── token_manager.py    # ECDSA capability tokens
│   ├── critic/
│   │   ├── arbiter.py      # Central Arbiter (governs leaf nodes)
│   │   └── nodes.py        # Reasoning, Injection, Safety, Quality critics
│   └── training/
│       ├── labeler.py      # Failure → labeling queue → training export
│       ├── calibration.py  # ECE (Expected Calibration Error) tracker
│       ├── evidential.py   # Uncertainty metadata enrichment for exports
│       └── scheduler.py    # Background auto-export daemon
└── models/
    ├── trace.py            # Append-only audit log
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
- **Tests** — pytest, `TestClient` for API tests, fixtures in `tests/conftest.py`. 175 tests across all phases.
- **Alembic** — `render_as_batch=True` for SQLite, migrations auto-generated

## Pipeline Flow (app/agent/pipeline.py)

```
Prompt
  → Step 1: Immune input scan (multi-language injection detection, memory bank,
             escalation tracker; prompt hardening on FLAG verdicts)
  → Step 2: A-S-FLC decision analysis (LLM path decomposition → system hint)
  → Step 3: LLM generation (Gemini / OpenAI / mock fallback)
  → Step 4: Arbiter critic evaluation (DB-backed + heuristic nodes)
      → If HALT: push to labeling queue, return error
  → Step 5: Covernor governance check (default-deny policy engine)
      → If DENY: return blocked
      → If REQUIRE_APPROVAL: create ApprovalRequest, issue ECDSA token on quorum
  → Step 6: Immune output scan (block leaked secrets)
  → Step 7: Return completed response + tamper-evident hash-chained trace
```

## Current State — All Phases Complete

**199 passing tests** across 14 test files.

**Completed phases:**
- **Phase 1**: Foundation — pipeline, models, immune scanner, arbiter, governance, tests
- **Phase 2**: Live LLM integration — Gemini/OpenAI/mock providers, streaming critic, `model_id`/`token_count` on traces
- **Phase 3**: DB-backed LLM critics — `Arbiter.load_from_registry()`, LLM reasoning/injection critics, re-evaluate endpoint
- **Phase 4**: Full governance — ECDSA tokens, K-of-N approval with quorum, hash-chained traces, approval expiration
- **Phase 5**: A-S-FLC integration — LLM path decomposition, asymmetric risk evaluation, system hints to guide generation
- **Phase 6**: Training flywheel — Doctrine Lab bridge, labeling API, ECE calibration, evidential loss enrichment, LoRA compare, scheduled export
- **Phase 7**: Agent-Immune upgrade — 11-language injection patterns, Semantic Memory Bank, PromptHardener, session escalation tracker

## Running

```bash
source venv/bin/activate
alembic upgrade head
uvicorn app.main:app --reload --port 9000
pytest tests/ -v
```

## Key Design Decisions

- **Default-deny governance**: Unknown actions are always denied. Policies must explicitly allow.
- **Critic tree, not flat scoring**: The Arbiter pattern allows adding/removing critic nodes at runtime via the DB.
- **Hash chain traces**: Each trace has `prev_hash` and `trace_hash` for tamper-evident audit logs.
- **[UNC] token insertion**: On rollback, the Arbiter inserts uncertainty markers during streaming evaluation.
- **ECDSA capability tokens**: After K-of-N approval, a signed single-use token is issued and verified cryptographically.
- **Prompt hardening**: Flagged (but not blocked) prompts have injection fragments stripped before LLM generation.
- **Memory-based detection**: Blocked attacks are added to the Semantic Memory Bank for fuzzy-matching future attempts.
- **Session escalation**: Repeated suspicious prompts in the same session accumulate score until the session is auto-blocked.

---

# Phase Specifications (All Complete)

## Phase 2 — Live LLM Integration

## Goal

Replace `_mock_generate()` with real Gemini/OpenAI calls. Add streaming critic evaluation. Persist `model_id` and `token_count` in traces. Keep mock as fallback when no API key is configured.

## Step-by-step instructions

### 1. Create `app/core/llm/__init__.py`
Empty file.

### 2. Create `app/core/llm/models.py`

```python
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class LLMResponse:
    text: str
    model_id: str
    token_count: int
    latency_ms: float
    provider: str  # "gemini", "openai", "mock"
    raw_response: Optional[dict] = None

@dataclass
class LLMChunk:
    text: str
    index: int
    is_final: bool = False
```

### 3. Create `app/core/llm/provider.py`

Unified LLM provider with these methods:
- `generate(prompt: str, model_id: str | None, system_prompt: str | None) -> LLMResponse`
- `generate_stream(prompt: str, model_id: str | None) -> Generator[LLMChunk]`

Implementation details:
- Read `settings.GEMINI_API_KEY`, `settings.GEMINI_MODEL`, `settings.OPENAI_API_KEY` from config.
- **Provider selection**: if `model_id` starts with `gpt` or `ft:` → OpenAI. If `model_id` starts with `gemini` → Gemini. If `model_id` is None: use Gemini if `GEMINI_API_KEY` is set, else OpenAI if `OPENAI_API_KEY` is set, else fall back to mock.
- **Gemini**: use `google.genai.Client(api_key=key)` → `client.models.generate_content(model=model_id, contents=prompt)`. Count tokens from `response.usage_metadata.total_token_count` if available.
- **OpenAI**: use `openai.OpenAI(api_key=key)` → `client.chat.completions.create(...)`. Count tokens from `response.usage.total_tokens`.
- **Mock fallback**: return the same static JSON that `_mock_generate()` currently returns in `pipeline.py`, with `provider="mock"` and `token_count=0`.
- **Retry**: wrap API calls with up to 3 retries on transient errors (rate limit, timeout), with exponential backoff. Use `tenacity` or a simple loop.
- **Token counting**: always populate `token_count` in the `LLMResponse`. If the API doesn't return it, estimate from `len(text.split())`.

### 4. Modify `app/agent/pipeline.py`

**Replace `_mock_generate` usage (line 84):**
```python
# Before:
response = _mock_generate(prompt, model_id)

# After:
from app.core.llm.provider import generate
llm_result = generate(prompt, model_id=model_id)
response = llm_result.text
```

**Add `model_id` and `token_count` to `PipelineResult`** (add fields to the dataclass around line 28-38):
```python
model_id_used: Optional[str] = None
token_count: Optional[int] = None
```

Set them after LLM generation:
```python
result.model_id_used = llm_result.model_id
result.token_count = llm_result.token_count
```

**Fix `_persist_trace` (lines 184-202)** — add these two fields to the `Trace()` constructor:
```python
model_id=result.model_id_used,
token_count=result.token_count,
```
These columns already exist on the `Trace` model (`trace.py` lines 53-55) but are currently never set.

**Keep `_mock_generate` function** — it's still useful as the fallback in the LLM provider. You can either keep it in `pipeline.py` or move it to `provider.py` as the mock path. Either way, the pipeline should NOT call it directly anymore.

### 5. Modify `app/core/critic/arbiter.py` — Add streaming evaluation

Add method to the `Arbiter` class (after the existing `evaluate` method, around line 124):

```python
def evaluate_stream(self, context: dict, chunks: list) -> ArbiterResult:
    """
    Evaluate a response that arrived as streaming chunks.
    Accumulates tokens up to CRITIC_CHUNK_SIZE, evaluates each batch.
    Inserts [UNC] on rollback, halts if max rollbacks exceeded.
    """
    accumulated = ""
    chunk_size = settings.CRITIC_CHUNK_SIZE  # default 64 tokens
    
    for chunk in chunks:
        accumulated += chunk.text
        words = accumulated.split()
        
        if len(words) >= chunk_size or chunk.is_final:
            eval_context = {**context, "response": accumulated}
            result = self.evaluate(eval_context)
            
            if result.verdict == "halt":
                return result
            
            if result.verdict == "rollback":
                accumulated += " [UNC] "
                if self._rollback_count > settings.CRITIC_MAX_ROLLBACKS:
                    return ArbiterResult(
                        verdict="halt",
                        scores=result.scores,
                        rollback_count=self._rollback_count,
                        halted_by="arbiter:max_rollbacks_streaming",
                        unc_inserted=True,
                    )
    
    # Final evaluation on complete text
    final_context = {**context, "response": accumulated}
    return self.evaluate(final_context)
```

### 6. Create tests

**`tests/test_llm_provider.py`:**
- Test Gemini provider with mocked `google.genai.Client` — mock `generate_content()` to return a fake response, verify `LLMResponse` has `text`, `model_id`, `token_count`, `provider="gemini"`.
- Test OpenAI provider with mocked `openai.OpenAI` — mock `chat.completions.create()`, verify response shape.
- Test mock fallback when no API keys are set — verify `provider="mock"`.
- Test retry on transient error (mock a rate-limit exception, verify it retries).

**Update `tests/test_pipeline.py`:**
- Add test `test_trace_has_model_fields`: run pipeline, query the Trace from DB, verify `model_id` and `token_count` are set (not None). When no API key is configured, `model_id` should be something like `"mock"` and `token_count` should be `0`.
- All existing 13 tests in this file must still pass. The mock fallback ensures they work even without API keys.

**`tests/test_streaming_critic.py`:**
- Test `evaluate_stream` with mock chunks that pass → verify `verdict="pass"`.
- Test `evaluate_stream` with a failing chunk → verify `[UNC]` inserted and rollback counted.
- Test max rollbacks reached → verify `verdict="halt"`.

### 7. Verify

```bash
pytest tests/ -v
```

All 56 existing tests + new tests must pass. The mock fallback ensures existing tests work without API keys. No Alembic migration needed (no schema changes — `model_id` and `token_count` columns already exist on `Trace`).

## Stop condition for Phase 2

- [ ] `_mock_generate()` is no longer called directly from the pipeline
- [ ] `LLMProvider` with Gemini, OpenAI, and mock fallback exists
- [ ] Pipeline sets `model_id` and `token_count` on every trace
- [ ] `Arbiter.evaluate_stream()` works with chunked evaluation and `[UNC]` rollback
- [ ] All 56 existing tests pass + new provider/streaming tests pass
- [ ] When `GEMINI_API_KEY` is set, the pipeline makes real LLM calls

---

# AFTER Phase 2: What comes next

## Phase 3: DB-Backed LLM Critics

- `Arbiter.load_from_registry(db_session)` reads active nodes from `critic_registry` table (model: `CriticNode` in `app/models/critic_registry.py` — has `prompt_template`, `threshold_pass`, `threshold_halt`, `can_halt`, `is_active`).
- Add `LLMReasoningCritic` / `LLMInjectionCritic` to `nodes.py` — call LLM provider with prompt template from DB. Keep heuristic classes as fast pre-filters.
- Create `app/services/replay.py` + `POST /api/traces/{id}/re-evaluate` for critic re-execution.
- IMPORTANT: `GET /api/traces/{id}/replay` already exists and is audit-only — do NOT modify it.

## Phase 4: Full Governance

- Replace HMAC with ECDSA in `token_manager.py`.
- Wire `APPROVAL_QUORUM` (config line 15, value 2) into K-of-N vote counting.
- Create `ApprovalRequest` rows when `require_approval` is triggered (pipeline lines 133-138).
- Compute `trace_hash` and `prev_hash` on every trace (model fields exist, lines 61-62 of `trace.py`).

## Phase 5: A-S-FLC Integration

- Create `app/core/asflc/analyzer.py` — LLM path decomposition.
- Wire into pipeline between input scan and LLM generation.
- `asflc_result/chosen_path/confidence/loops` columns already exist on Trace (lines 32-35).

## Phase 6: Training Flywheel + Doctrine Lab Bridge

- HTTP client to Doctrine Lab (runs on port 8000). Config already has `DOCTRINE_LAB_URL` and `DOCTRINE_LAB_API_KEY`.
- Call `POST /api/datasets/import` to send failure traces (endpoint already built in Doctrine Lab).
- Call `POST /api/eval/report` for benchmarks (rate limited: 3/min).
- Call `POST /api/finetune/openai/start` to trigger fine-tuning.
- Auth: send `X-API-Key` header with `DOCTRINE_LAB_API_KEY`.
- Idempotency: tag exports with `batch_id` (hash of trace IDs).

## Phase 7: Agent-Immune Upgrade ✓

- 11-language injection patterns (English, Spanish, French, German, Portuguese, Chinese, Japanese, Korean, Russian, Arabic, Hindi).
- Semantic Memory Bank: token-set Jaccard similarity matching for known attack signatures.
- PromptHardener: strips injection fragments from flagged (non-blocked) prompts before LLM generation.
- Session-based EscalationTracker: accumulates per-session threat scores with time-decay, auto-blocks escalated sessions.
- Pipeline wired: `scan_input(prompt, session_id=...)` feeds escalation tracking, `harden_prompt()` applied on FLAG verdicts.
