# AGENTS.md — Nexus Agent

## What This Project Is

Nexus Agent is a **Zero-Trust & Self-Evolving AI Agent System**. It wraps LLM calls in strict security boundaries (input scanning, governance approval, output scanning) and uses a critic tree to evaluate every generation. Failures feed back into a labeling queue for continuous fine-tuning.

**Sister project**: [Doctrine Lab](../thinking-DT/doctrine-lab/) provides the dataset generation, curation, fine-tuning pipeline, and benchmark evaluation. Nexus Agent is the runtime; Doctrine Lab is the training factory.

**Doctrine Lab Milestone 1 (B.1 + B.2) is COMPLETE**: Doctrine Lab now has agent-safety eval tasks (`agent_safety`, `agent_reasoning`, `agent_governance`, `injection_resistance`) and a `POST /api/datasets/import` endpoint ready to receive Nexus failure traces.

## Tech Stack

- **Python 3.13**, FastAPI, SQLAlchemy 2.0, Alembic, Pydantic v2
- **Database**: SQLite (dev), PostgreSQL (prod)
- **AI providers**: Google Gemini (`google-genai`), OpenAI (`openai`), DeepSeek (OpenAI-compatible) — all in `requirements.txt`
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
- **Tests** — pytest, `TestClient` for API tests, fixtures in `tests/conftest.py`. 207 tests across 14 test files.
- **Alembic** — `render_as_batch=True` for SQLite, migrations auto-generated

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

## Current State — All Phases Complete

**207 passing tests** across 14 test files.

**Completed phases:**
- **Phase 1**: Foundation — pipeline, models, immune scanner, arbiter, governance, tests
- **Phase 2**: Live LLM integration — Gemini/OpenAI/DeepSeek/mock providers, streaming critic, `model_id`/`token_count` on traces
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


See `PROJECT_PLAN.md` for full phase details, database schema reference, and API endpoint documentation.
