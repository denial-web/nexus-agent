# Nexus Agent

**Zero-Trust & Self-Evolving AI Agent System**

A production-grade agent runtime that wraps every LLM call in strict security boundaries — input scanning, governance approval, critic evaluation, output scanning — and feeds failures back into a labeling queue for continuous fine-tuning.

```
┌──────────────────────────────────────────────────────────┐
│                     GATEWAY LAYER                        │
│  Agent-Immune (input scan) → Covernor (output firewall)  │
├──────────────────────────────────────────────────────────┤
│                      BRAIN LAYER                         │
│  A-S-FLC (decision engine) ← Arbiter (critic tree)      │
├──────────────────────────────────────────────────────────┤
│                    FLYWHEEL LAYER                        │
│  Failure traces → Labeling queue → Fine-tune → Deploy    │
└──────────────────────────────────────────────────────────┘
```

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [How It Works](#how-it-works)
3. [Configuration](#configuration)
4. [API Reference](#api-reference)
5. [Dashboard](#dashboard)
6. [Deployment](#deployment)
7. [Development](#development)
8. [Architecture Deep Dive](#architecture-deep-dive)

---

## Quick Start

### Prerequisites

- Python 3.13+
- (Optional) PostgreSQL 16 for production
- (Optional) Docker & Docker Compose

### Install & Run

```bash
# Clone and set up
git clone https://github.com/denial-web/nexus-agent.git
cd nexus-agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env — add at least one AI provider key (GEMINI_API_KEY, OPENAI_API_KEY, or DEEPSEEK_API_KEY)

# Start
make dev
# or manually: alembic upgrade head && uvicorn app.main:app --reload --port 9000
```

The server starts at **http://localhost:9000**. Without any API keys configured, Nexus Agent runs in **mock mode** — it returns deterministic responses so you can explore the full pipeline without spending API credits.

### Your First Request

```bash
curl -X POST http://localhost:9000/api/agent/run \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is quantum computing?"}'
```

Response:

```json
{
  "trace_id": "a1b2c3d4...",
  "session_id": "e5f6g7h8...",
  "status": "completed",
  "response": "...",
  "model_id": "gemini-2.5-flash",
  "token_count": 142,
  "pipeline": {
    "immune_input": {"verdict": "pass", "score": 0.0},
    "asflc": null,
    "critic": {"verdict": "pass", "scores": [...]},
    "governance": {"status": "auto", "decision": "allow"},
    "immune_output": {"verdict": "pass"}
  },
  "latency_ms": 1823.4,
  "error": null
}
```

Every response includes the full pipeline audit trail — what the immune scanner found, what the critic tree scored, what governance decided.

---

## How It Works

Every prompt goes through a **7-step zero-trust pipeline**:

```
Your Prompt
  │
  ▼
┌─────────────────────────────────────────┐
│ Step 1: IMMUNE INPUT SCAN               │
│ Detects injection attacks in 11         │
│ languages. Blocks, flags, or passes.    │
│ Flagged prompts get hardened (injection  │
│ fragments stripped) before generation.   │
└──────────────┬──────────────────────────┘
               ▼
┌─────────────────────────────────────────┐
│ Step 2: A-S-FLC DECISION ANALYSIS       │
│ LLM decomposes the request into         │
│ alternative decision paths with          │
│ probabilistic risk evaluation.           │
│ Produces a system hint for generation.   │
│ (Skipped for short/simple prompts)       │
└──────────────┬──────────────────────────┘
               ▼
┌─────────────────────────────────────────┐
│ Step 3: LLM GENERATION                  │
│ Gemini / OpenAI / DeepSeek / local HF   │
│ Mock fallback when no keys configured.   │
│ Retry with exponential backoff.          │
└──────────────┬──────────────────────────┘
               ▼
┌─────────────────────────────────────────┐
│ Step 4: ARBITER CRITIC EVALUATION       │
│ DB-backed critic tree scores the        │
│ response: Reasoning, Injection, Safety, │
│ Quality nodes. HALT = blocked.          │
└──────────────┬──────────────────────────┘
               ▼
┌─────────────────────────────────────────┐
│ Step 5: COVERNOR GOVERNANCE CHECK       │
│ Default-deny policy engine. Actions     │
│ need explicit policies to be allowed.   │
│ High-risk = K-of-N human approval       │
│ + ECDSA capability token on quorum.     │
└──────────────┬──────────────────────────┘
               ▼
┌─────────────────────────────────────────┐
│ Step 6: IMMUNE OUTPUT SCAN              │
│ Blocks leaked secrets, system prompts,  │
│ or sensitive data in the response.      │
└──────────────┬──────────────────────────┘
               ▼
┌─────────────────────────────────────────┐
│ Step 7: RETURN RESPONSE                 │
│ Tamper-evident hash-chained trace       │
│ stored in the audit log.                │
└─────────────────────────────────────────┘
```

**If anything goes wrong at any step**, the failure is captured in the **labeling queue** — ready for human review and eventual fine-tuning.

---

## Configuration

All settings are controlled via environment variables (or a `.env` file). Copy `.env.example` to `.env` and edit.

### AI Providers

Set at least one to use real LLM generation. Without any keys, Nexus Agent runs in mock mode.

| Variable | Description | Default |
|----------|-------------|---------|
| `GEMINI_API_KEY` | Google Gemini API key | (empty) |
| `GEMINI_MODEL` | Gemini model name | `gemini-2.5-flash` |
| `OPENAI_API_KEY` | OpenAI API key | (empty) |
| `OPENAI_MODEL` | OpenAI model name | `gpt-4o-mini` |
| `DEEPSEEK_API_KEY` | DeepSeek API key | (empty) |
| `DEEPSEEK_MODEL` | DeepSeek model name | `deepseek-chat` |

Provider selection: if you set `model_id` in your request, it routes based on the prefix (`gpt` → OpenAI, `gemini` → Gemini, `deepseek` → DeepSeek). Without an explicit `model_id`, it tries Gemini → OpenAI → DeepSeek → mock, using whichever has a key set.

### Security

| Variable | Description | Default |
|----------|-------------|---------|
| `NEXUS_API_KEY` | API key for all endpoints. Empty = no auth (dev mode). | (empty) |
| `SESSION_SECRET` | Dashboard session signing key. **Required in production.** | (empty) |
| `RATE_LIMIT_RPM` | Max requests/min to expensive endpoints per IP. 0 = unlimited. | `30` |
| `MAX_PROMPT_LENGTH` | Max prompt characters. 0 = unlimited. | `50000` |
| `ENFORCE_DASHBOARD_CSRF` | Enable CSRF protection on dashboard forms. | `false` |

Generate secrets with:
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### Governance

| Variable | Description | Default |
|----------|-------------|---------|
| `APPROVAL_QUORUM` | Minimum approvers for K-of-N governance | `2` |
| `ECDSA_PRIVATE_KEY_PATH` | Path to ECDSA private key PEM. Auto-generated if empty. | (empty) |

### Database

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_URL` | SQLAlchemy database URL | `sqlite:///./nexus.db` |

For PostgreSQL: `postgresql://user:pass@host:5432/nexus_db`

### Data Retention

| Variable | Description | Default |
|----------|-------------|---------|
| `RETENTION_TRACE_DAYS` | Auto-delete traces older than N days. 0 = keep forever. | `0` |
| `RETENTION_LABELING_DAYS` | Auto-delete exported labeling items older than N days. | `0` |
| `RETENTION_APPROVAL_DAYS` | Auto-delete resolved approvals older than N days. | `0` |
| `RETENTION_CALIBRATION_DAYS` | Auto-delete calibration snapshots older than N days. | `0` |

### Observability

| Variable | Description | Default |
|----------|-------------|---------|
| `EXPOSE_METRICS` | Enable Prometheus `/metrics` endpoint. | `false` |
| `LOG_LEVEL` | Python log level (DEBUG, INFO, WARNING, ERROR). | `INFO` |
| `ENVIRONMENT` | `development`, `staging`, or `production`. Controls log format (JSON in prod, text in dev). | `development` |

---

## API Reference

All endpoints require `X-API-Key` header when `NEXUS_API_KEY` is set. Interactive docs at **http://localhost:9000/docs** (Swagger) or **/redoc**.

### Agent Execution

#### `POST /api/agent/run` — Run the pipeline

```bash
curl -X POST http://localhost:9000/api/agent/run \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-key" \
  -d '{
    "prompt": "Explain neural networks",
    "session_id": "optional-session-id",
    "model_id": "gemini-2.5-flash"
  }'
```

- `session_id` — groups related prompts for escalation tracking and hash chaining
- `model_id` — override the LLM provider (`gpt-4o-mini`, `gemini-2.5-flash`, `deepseek-chat`, etc.)

#### `POST /api/agent/stream` — Stream tokens via SSE

Same request body as `/run`. Returns Server-Sent Events:

```bash
curl -N -X POST http://localhost:9000/api/agent/stream \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Write a haiku about AI"}'
```

Events:
- `event: status` — pipeline stage (`input_scan`, `generating`, `evaluating`)
- `event: token` — `{"text": "...", "index": 0}`
- `event: done` — `{"trace_id": "...", "status": "completed", "latency_ms": 1234}`
- `event: error` — `{"status": "blocked", "error": "..."}`

#### `POST /api/agent/compare` — Multi-model comparison

Run the same prompt through multiple models, score each with the critic tree, and pick the best:

```bash
curl -X POST http://localhost:9000/api/agent/compare \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Explain quantum entanglement",
    "model_ids": ["gemini-2.5-flash", "gpt-4o-mini"]
  }'
```

Returns all candidates with scores, plus the winner. If `model_ids` is omitted, all configured providers are used.

### Traces (Audit Log)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/traces` | List traces (supports `?session_id=`, `?status=`, `?limit=`, `?offset=`) |
| `GET` | `/api/traces/{id}` | Full trace details |
| `GET` | `/api/traces/{id}/replay` | Step-by-step pipeline replay |
| `POST` | `/api/traces/{id}/re-evaluate` | Re-run current critic tree on a stored trace |
| `GET` | `/api/traces/session/{session_id}/verify-chain` | Verify hash chain integrity |

### Governance

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/governance/policies` | List governance policies |
| `POST` | `/api/governance/policies` | Create a new policy |
| `GET` | `/api/governance/approvals` | List pending approval requests |
| `POST` | `/api/governance/approve/{request_id}` | Submit an approval/denial vote |

#### Governance Policies

Policies control what the agent is allowed to do. The system is **default-deny** — if no policy matches, the action is blocked.

```bash
# Create a policy that allows "analyze" actions on "data" resources
curl -X POST http://localhost:9000/api/governance/policies \
  -H "Content-Type: application/json" \
  -d '{
    "name": "allow-data-analysis",
    "action_pattern": "analyze",
    "resource_pattern": "data",
    "decision": "allow",
    "risk_level": "low",
    "required_approvals": 0,
    "priority": 10
  }'
```

Policy decisions: `allow`, `deny`, `require_approval`

#### Approval Flow

When a policy requires approval:

1. The pipeline creates an `ApprovalRequest` and returns `status: "pending_approval"` with an `approval_request_id`
2. Human approvers submit votes via the API or dashboard
3. Once quorum is reached (default: 2 approvers), an ECDSA capability token is minted
4. The trace is completed and the response is released

```bash
# Vote to approve
curl -X POST http://localhost:9000/api/governance/approve/REQUEST_ID \
  -H "Content-Type: application/json" \
  -d '{"approver_id": "alice", "decision": "approve"}'
```

### Critic Registry

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/critic/registry` | List registered critic nodes |
| `POST` | `/api/critic/registry` | Register or update a critic node |
| `PATCH` | `/api/critic/registry/{name}` | Update a critic node |

Critic nodes are hot-swappable — you can add, update, or deactivate them at runtime without restarting the server.

### Training Flywheel

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/training/queue` | View labeling queue (`?status=pending&failure_type=safety&limit=50`) |
| `POST` | `/api/training/queue/{item_id}/label` | Apply a human label to a queued item |
| `POST` | `/api/training/export` | Export labeled items as training data |
| `POST` | `/api/training/eval` | Submit eval report to Doctrine Lab |
| `POST` | `/api/training/finetune` | Trigger fine-tuning via Doctrine Lab |
| `GET` | `/api/training/finetune/status/{job_id}` | Check fine-tuning job status |
| `POST` | `/api/training/promote-adapter` | Promote a LoRA adapter to active critic |
| `POST` | `/api/training/lora/compare` | Compare critic before/after LoRA swap |
| `POST` | `/api/training/calibration/persist` | Persist a calibration snapshot |
| `GET` | `/api/training/calibration/snapshots` | List calibration snapshots |

### Health & Metrics

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness probe |
| `GET` | `/health/ready` | Readiness probe (checks DB connectivity) |
| `GET` | `/metrics` | Prometheus metrics (opt-in via `EXPOSE_METRICS=true`) |

---

## Dashboard

A browser-based UI at **http://localhost:9000/dashboard** with four sections:

### Trace Explorer
Browse all execution traces with status counts. Click any trace to see full details — prompt, response, every pipeline step, critic scores, and governance decisions.

### Labeling Queue
Review failure traces queued for training. Apply labels: `correct_flag`, `incorrect`, `false_positive`, `needs_review`. Labeled items are automatically exported to the training flywheel.

### Approval Console
View pending approval requests and cast votes (approve/deny) directly from the browser.

### Calibration Dashboard
Monitor Expected Calibration Error (ECE) across critic nodes. See accuracy-vs-confidence bins to understand how well your critics are calibrated.

When `NEXUS_API_KEY` is set, the dashboard requires login. In development mode (no API key), it's open.

---

## Deployment

### Docker (SQLite)

```bash
cp .env.example .env
# Edit .env with your settings
make docker-up
# or: docker compose up --build -d
```

### Docker (PostgreSQL)

```bash
docker compose --profile postgres up --build -d
```

This starts both the app and a PostgreSQL 16 container.

### Production Checklist

1. **Set `ENVIRONMENT=production`** — enables JSON structured logging and startup checks
2. **Set `NEXUS_API_KEY`** — required; app refuses to start without it in production
3. **Set `SESSION_SECRET`** — required for dashboard session security
4. **Set `ENFORCE_DASHBOARD_CSRF=true`** — CSRF protection for dashboard forms
5. **Use PostgreSQL** — SQLite is for development only
6. **Configure retention** — set `RETENTION_*_DAYS` to prevent unbounded data growth
7. **Enable metrics** — set `EXPOSE_METRICS=true` and scrape `/metrics` with Prometheus

### Multi-Worker Deployment

Rate limiting, capability tokens, and the training scheduler use in-process memory. For `uvicorn --workers > 1`:

- Set `NEXUS_SKIP_SCHEDULER=1` on all workers except one (prevents duplicate background jobs)
- For accurate rate limiting across workers, replace with a Redis-backed limiter
- Capability tokens are verified in the same process that issued them; for cross-worker verification, persist tokens in the database

---

## Development

### Common Commands

```bash
make dev          # Install deps, migrate, start with hot-reload
make test         # Run all 384 tests
make test-fast    # Run tests, stop on first failure
make test-cov     # Run tests with coverage report (terminal + htmlcov/)
make lint         # Check code with ruff
make format       # Auto-format with ruff
make typecheck    # Run mypy type checking
make audit        # Security audit dependencies
make migrate      # Run database migrations
make migration    # Create a new migration (interactive)
make clean        # Remove caches and test databases
```

### Running Tests

```bash
# Full suite (SQLite)
make test

# Against PostgreSQL
TEST_DATABASE_URL="postgresql://user:pass@localhost:5432/nexus_test" \
DATABASE_URL="postgresql://user:pass@localhost:5432/nexus_test" \
pytest tests/ -v

# Single test file
pytest tests/test_pipeline.py -v

# Single test
pytest tests/test_pipeline.py::TestPipeline::test_clean_prompt_completes -v
```

### Project Structure

| Directory | Purpose |
|-----------|---------|
| `app/agent/` | Pipeline orchestrator — the core of the system |
| `app/api/` | FastAPI route handlers |
| `app/core/immune/` | Injection detection (11 languages), memory bank, prompt hardening |
| `app/core/asflc/` | A-S-FLC decision framework — asymmetric risk evaluation |
| `app/core/llm/` | LLM provider abstraction (Gemini, OpenAI, DeepSeek, local, mock) |
| `app/core/covernor/` | Governance engine — policies, approvals, ECDSA tokens |
| `app/core/critic/` | Arbiter critic tree — pluggable evaluation nodes |
| `app/core/training/` | Training flywheel — labeling, calibration, export, scheduling |
| `app/services/` | Cross-cutting services — integrity, replay, retention, Doctrine Lab bridge |
| `app/models/` | SQLAlchemy models |
| `app/templates/` | Jinja2 templates for the dashboard |
| `tests/` | 384 tests across 21 files |

---

## Architecture Deep Dive

### Core Components

**Agent-Immune Scanner** (`app/core/immune/scanner.py`)
Detects prompt injection attacks across 11 languages (English, Chinese, Russian, Arabic, Hindi, Japanese, Korean, Spanish, French, German, Portuguese). Uses a Semantic Memory Bank to remember previously blocked attacks and fuzzy-match new ones. Session escalation tracks repeated suspicious prompts. Flagged prompts are hardened (injection fragments stripped) before LLM generation.

**A-S-FLC Decision Engine** (`app/core/asflc/`)
Asymmetric-Subjective Fuzzy Logic Controller. For complex prompts, uses the LLM to decompose the request into alternative decision paths, evaluates each with asymmetric risk treatment (negative outcomes are penalized harder), and produces a system hint that guides the main generation. Skipped for short/simple prompts.

**Arbiter Critic Tree** (`app/core/critic/`)
Evaluates every LLM response through pluggable critic nodes loaded from the database. Each node (Reasoning, Injection, Safety, Quality) scores the response. Nodes can be added, updated, or deactivated at runtime. If any halt-capable node triggers, the response is blocked and the failure is queued for human review.

**Covernor Governance** (`app/core/covernor/`)
Default-deny policy engine. Every action must be explicitly allowed by a policy. High-risk actions require K-of-N human approval — once quorum is reached, an ECDSA capability token is cryptographically minted as proof of authorization.

**Training Flywheel** (`app/core/training/`)
Every failure (critic halt, governance denial, immune block) is pushed to a labeling queue. Human reviewers assign labels via the dashboard or API. Labeled items are exported as training data, optionally enriched with evidential uncertainty metadata, and sent to Doctrine Lab for fine-tuning. A background scheduler handles automated exports and calibration snapshots.

### Hash Chain Integrity

Every trace is linked to the previous trace in its session via `prev_hash` → `trace_hash`, forming a tamper-evident chain. Verify chain integrity via:

```bash
curl http://localhost:9000/api/traces/session/YOUR_SESSION_ID/verify-chain
```

### Sister Project: Doctrine Lab

[Doctrine Lab](https://github.com/denial-web/doctrine-lab) is the training factory that complements Nexus Agent:
- Nexus Agent generates failure traces → exports them as training data
- Doctrine Lab curates datasets, runs fine-tuning, and evaluates models
- Fine-tuned LoRA adapters are hot-swapped back into Nexus Agent's critic nodes

---

## License

Licensed under the [Apache License 2.0](LICENSE).
