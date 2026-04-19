# Nexus Agent

[![CI](https://github.com/denial-web/nexus-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/denial-web/nexus-agent/actions/workflows/ci.yml)
[![Nightly Benchmarks](https://github.com/denial-web/nexus-agent/actions/workflows/nightly_benchmark.yml/badge.svg)](https://github.com/denial-web/nexus-agent/actions/workflows/nightly_benchmark.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/Python-3.13+-3776AB.svg)](https://www.python.org/)
[![Tests: 1400](https://img.shields.io/badge/Tests-1400_passing-brightgreen.svg)](tests/)
[![Demo](https://img.shields.io/badge/🤗_Demo-Live-orange.svg)](https://huggingface.co/spaces/denialkhmbot/nexus-agent-demo)

**A governed AI agent with built-in audit and learning.**

Nexus is a complete agent system — tools, planning, memory, and self-improving skills — wrapped in a zero-trust runtime that enforces security, governance, and audit on every action. Every LLM call, tool execution, and memory write passes through the same pipeline — **scan → decision → generation → critic → governance → audit** — and answers *"why did I do X?"* with a cryptographically-signed audit chain.

**Nexus runs on its own.** It also interoperates with existing agent ecosystems:

- Imports [OpenClaw](https://www.clawhub.ai/) SKILL.md skills
- Routes [Hermes](https://huggingface.co/NousResearch)-class tool-calling models
- Works with any LLM provider (Gemini, OpenAI, DeepSeek, Ollama, local HF)

Nexus does not replace models or skill ecosystems. **It replaces the missing control layer** that makes agents safe, auditable, and production-ready.

Concretely: prompt-injection scanning in 11 languages, default-deny governance with K-of-N approval, a pluggable critic tree, bitemporal memory with causal provenance, and a training flywheel that turns every failure into a labeled training example.

```
┌──────────────────────────────────────────────────────────┐
│                     GATEWAY LAYER                        │
│  Agent-Immune (input scan) → Covernor (output firewall)  │
├──────────────────────────────────────────────────────────┤
│                      BRAIN LAYER                         │
│  A-S-FLC (decision engine) ← Arbiter (critic tree)       │
├──────────────────────────────────────────────────────────┤
│                      MEMORY LAYER                        │
│  Bitemporal beliefs + Beta confidence + causal DAG       │
│  + per-user hash chain. Default-deny writes.             │
├──────────────────────────────────────────────────────────┤
│                    FLYWHEEL LAYER                        │
│  Failure traces → Labeling queue → Fine-tune → Deploy    │
└──────────────────────────────────────────────────────────┘
```

**[Try the live demo on HuggingFace](https://huggingface.co/spaces/denialkhmbot/nexus-agent-demo)** — no install needed, runs in your browser.

> **Demo note.** The HuggingFace Space runs the governance pipeline in **mock mode** and demonstrates the security layer only (input scan, critic, output scan, hash chain). Full agent behaviour — planning, skills, bitemporal memory — requires live LLM calls and is shown in the reproducible tests and `nexus chat` CLI demos below. Both claims are separately falsifiable; the Space is load-bearing for the *governance* claim, not the *complete-agent* claim.

**[See the benchmark numbers](docs/benchmarks.md)** — six nightly benchmarks covering bitemporal recall, contradiction handling, causal derivation, tool-call injection, skill composition (with a hostile probe), and end-to-end memory uplift in the agent loop. All six pass their exit gates on the current main.

**[Memory architecture](docs/memory.md)** — how beliefs are written, retrieved, and audited under the same zero-trust contract as every other Nexus surface (bitemporal + Beta confidence + causal DAG + hash-chained integrity).

---

## What Nexus is — and what it is not

**Nexus is the runtime.** It sits in front of the LLM call (any provider), in front of the tool call (any MCP backend), and in front of memory writes (any scope). Every one of those calls passes through the same zero-trust pipeline: immune scan → decision analysis → generation → critic tree → governance → output scan → hash-chained trace.

| | Nexus is… | Nexus is not… |
|---|---|---|
| **vs OpenClaw / ClawHub** | The runtime that imports any SKILL.md (`POST /v1/skills/import`) and executes each step with Covernor-gated tool calls and a hash-chain audit. | A skill marketplace. OpenClaw already exists and is good. |
| **vs Hermes / tool-calling LLMs** | The runtime that any Hermes-class model plugs into via `LOCAL_HF_MODEL_ID` or the provider chain. Adds governance, critic arbitration, memory, and audit. | A function-calling LLM. Hermes already exists and is good. |
| **vs Mem0 / memory stores** | A bitemporal, Beta-confident, causally-provenanced, per-user-hash-chained memory layer — *governed by the same pipeline as everything else.* | A standalone memory provider. Not benchmarked against LoCoMo or LongMemEval. Not our fight. |
| **vs LangChain / CrewAI / AutoGen** | A zero-trust runtime you can put in front of any agent stack. | Another prompt-chaining framework. |

The bet: most agent stacks ship the brain (LLM + tools + memory) but skip the hard parts — governance, adversarial resilience, audit, and a real learning loop. Nexus is those hard parts, shipped as a runtime, wired into every primitive you already use.

---

## Nexus is also a complete agent on its own

You do not need OpenClaw skills or a Hermes-class model to use Nexus. Out of the box, Nexus ships everything a modern agent framework ships — plus governance, audit, and learning that most of them don't.

**These capabilities are not theoretical.** Every row below maps to a specific module path on `main`, and the [Killer demos](#killer-demos) section reproduces the four most load-bearing claims with single-command `pytest` runs on a fresh clone.

| Capability | What's in the box | Where it lives |
|---|---|---|
| **Tools** | `shell_exec`, `file_read`, `file_write`, `web_fetch`, `search` — all Covernor-gated by default | `app/core/agent/builtin.py` + extensible `ToolRegistry` |
| **ReAct loop** | JSON tool protocol, reflection, self-correction, resume-after-approval | `app/agent/agent_loop.py` |
| **Planning** | A-S-FLC decision engine decomposes the task into paths before generation | `app/core/asflc/` |
| **Self-improving skills** | Auto-abstracted from high-reward trajectories. Reward-tracked, auto-disabled if they drift. | `Skill` model + `_retrieve_skills` |
| **Episode memory** | Past similar tasks surface in the system prompt, ranked by reward | `Episode` model + `_retrieve_episodes` |
| **Belief memory** | Bitemporal, Beta-confident, causally provenanced, hash-chained per user | `app/core/memory/` + [docs/memory.md](docs/memory.md) |
| **LLM routing** | Gemini / OpenAI / DeepSeek / Ollama / local HF / mock, with circuit breakers and provider fallback | `app/core/llm/provider.py` |
| **Surfaces** | HTTP (`/v1/agent/run`, `/v1/agent/stream`, `/v1/agent/compare`), `nexus` CLI (`chat`, `run`, `memory`, `skills`), Telegram bot | `app/api/`, `app/cli.py`, `app/channels/` |

**Interoperability is a feature, not a dependency.** Nexus grows its own skills from its own trajectories and routes to whichever LLM you have keys for. Importing an OpenClaw SKILL.md or pointing `LOCAL_HF_MODEL_ID` at a Hermes-class model just means you don't have to rebuild what those ecosystems already do well — every Nexus primitive works without either.

---

## Killer demos

These are the four things Nexus does that nothing else in the OSS agent ecosystem does end-to-end. Each one is a single-command reproduction on a fresh clone.

### 1. "Safely run any OpenClaw skill"

Import a SKILL.md from ClawHub and run it. Every step passes through the immune scanner and Covernor. A hostile skill that tries `cat /etc/passwd > leaked.txt` imports cleanly (matching OpenClaw's flow) but is blocked *at execution step*, not just at static import.

```bash
nexus skills import --from-url https://clawhub.ai/skills/data-summarizer/SKILL.md
nexus skills list
nexus skills execute <skill_id>
# Full audit chain visible at /dashboard/traces
```

Backed by the `skill_composition` benchmark: **11/11 checks** (3 benign skills × {imported, executed, artifact_present} + 1 hostile × {imported, exec_denied}). See [docs/benchmarks.md](docs/benchmarks.md#5-skill_composition--clawhub-chain--hostile-probe).

### 2. "Why did I do X?"

Every belief the agent forms cites the beliefs it was derived from. `GET /v1/memory/{id}/explain` returns the derivation DAG as JSON and as rendered mermaid in the dashboard. Nobody else in OSS ships this.

```bash
curl http://localhost:9000/v1/memory/<belief_id>/explain
# → { "belief": {…}, "ancestors": [{…}, {…}],
#     "mermaid": "graph TD; A-->B; B-->C;" }
```

Backed by `causal_qa`: 3-level DAG (3 roots → 2 mid → 1 leaf), closure reaches every root, zero cycles.

### 3. "The agent changed its mind"

Say "my dog Rex is 3 years old" today. Say "Rex is 4 now" a year later. The skepticism layer detects the contradiction, updates the Beta confidence, stamps `superseded_at` on the old belief, and writes a new row — in the same DB transaction, on the same hash chain.

```bash
nexus memory history <belief_id>
# → v1: "rex.age = 3"  observed 2025-04-17 → superseded 2026-04-17
#   v2: "rex.age = 4"  observed 2026-04-17 → live
#   contradicts: [v1]
```

Backed by `contradiction_qa`: every skepticism verdict (accept / reject / supersede / needs_evidence), byte-for-byte hash reproduction, single-byte tamper detection.

### 4. "Adversarial tool injection blocked"

Hit the MCP tool-call boundary with 18 attacks across 10 categories (role override, multilingual, Unicode obfuscation, secret exfil, shell smuggling, nested payloads, hypotheticals, tool chaining, schema override, compound). **100% block rate** — and writing the red-team surfaced two real bugs in `app/core/mcp/proxy.py` that shipped with the benchmark.

```bash
python -m tests.eval.tool_injection_redteam --json
# → {"passes_exit_gate": true, "block_rate": 1.000, "attacks": 18, "blocks": 18}
```

Backed by `tool_injection_redteam`. The blocked attacks go to the labeling queue and feed the training flywheel.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [How It Works](#how-it-works)
3. [Memory](#memory)
4. [OpenClaw / ClawHub integration](#openclaw--clawhub-integration)
5. [Hermes-class model integration](#hermes-class-model-integration)
6. [Digital Employee (agentic mode)](#digital-employee-agentic-mode)
7. [Configuration](#configuration)
8. [API Reference](#api-reference)
9. [Dashboard](#dashboard)
10. [Deployment](#deployment)
11. [Development](#development)
12. [Architecture Deep Dive](#architecture-deep-dive)
13. [Contributing](#contributing)

---

## Quick Start

### Prerequisites

- Python 3.13+
- (Optional) PostgreSQL 16 for production
- (Optional) Docker & Docker Compose

### Install & Run

```bash
git clone https://github.com/denial-web/nexus-agent.git
cd nexus-agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env — add at least one AI provider key (GEMINI_API_KEY, OPENAI_API_KEY, or DEEPSEEK_API_KEY)

make dev
# or manually: alembic upgrade head && uvicorn app.main:app --reload --port 9000
```

The server starts at **http://localhost:9000**. Without any API keys configured, Nexus runs in **mock mode** — deterministic responses so you can explore the full pipeline without spending API credits.

### Your First Request

```bash
curl -X POST http://localhost:9000/v1/agent/run \
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

### Try the Injection Demo

See the Agent-Immune scanner block and flag attacks across languages:

```bash
python examples/injection_demo.py
```

Safe prompts pass through. Injections are **blocked** (English, highest confidence) or **flagged** and hardened (injection fragments stripped before reaching the LLM). More examples in [`examples/`](examples/).

---

## How It Works

Every prompt goes through a **zero-trust pipeline**. When `MEMORY_ENABLED=true`, memory retrieval runs before generation and governed belief writes run after.

```
Your Prompt
  │
  ▼
┌─────────────────────────────────────────┐
│ Step 1: IMMUNE INPUT SCAN               │
│ 11-language injection detection,        │
│ semantic memory bank of past attacks,   │
│ session escalation tracker, prompt      │
│ hardening on FLAG verdicts.             │
└──────────────┬──────────────────────────┘
               ▼
┌─────────────────────────────────────────┐
│ Step 1.5: MEMORY RECALL (optional)      │
│ RRF over 5 signals: semantic, lexical,  │
│ entity, episodic, confidence. Top-k     │
│ beliefs injected into the system hint.  │
└──────────────┬──────────────────────────┘
               ▼
┌─────────────────────────────────────────┐
│ Step 2: A-S-FLC DECISION ANALYSIS       │
│ LLM decomposes the request into         │
│ alternative paths with asymmetric risk  │
│ evaluation (negative outcomes penalized │
│ harder). Skipped for short prompts.     │
└──────────────┬──────────────────────────┘
               ▼
┌─────────────────────────────────────────┐
│ Step 3: LLM GENERATION                  │
│ Gemini / OpenAI / DeepSeek / Ollama /   │
│ local HF (Hermes-compatible). Circuit   │
│ breaker with automatic fallback chain.  │
└──────────────┬──────────────────────────┘
               ▼
┌─────────────────────────────────────────┐
│ Step 4: ARBITER CRITIC EVALUATION       │
│ DB-backed critic tree: Reasoning,       │
│ Injection, Safety, Quality nodes.       │
│ HALT ⇒ labeling queue.                  │
└──────────────┬──────────────────────────┘
               ▼
┌─────────────────────────────────────────┐
│ Step 5: COVERNOR GOVERNANCE CHECK       │
│ Default-deny policy engine. High-risk   │
│ actions require K-of-N human approval   │
│ + single-use ECDSA capability token.    │
└──────────────┬──────────────────────────┘
               ▼
┌─────────────────────────────────────────┐
│ Step 6: IMMUNE OUTPUT SCAN              │
│ Block leaked secrets, system prompts,   │
│ exfiltrated data.                       │
└──────────────┬──────────────────────────┘
               ▼
┌─────────────────────────────────────────┐
│ Step 7: HASH-CHAINED TRACE + MEMORY     │
│ Tamper-evident trace persisted. If      │
│ memory enabled: extractor → immune      │
│ scan → skepticism → Covernor →          │
│ governed belief write.                  │
└─────────────────────────────────────────┘
```

**If anything fails at any step**, the failure is captured in the **labeling queue** — ready for human review and eventual fine-tuning.

---

## Memory

Nexus ships a first-class memory layer that is *not* a vector DB and *not* a Mem0 competitor. It is a bitemporal belief store with Beta-distributed confidence, a causal derivation DAG, and a per-user tamper-evident hash chain — all governed by the same pipeline as every other write.

| Capability | What it means | Where to look |
|---|---|---|
| **Bitemporal** | Every belief tracks `observed_at` (when learned) and `superseded_at` (when stopped believing). Point-in-time queries are exact. | [`beliefs_as_of()`](app/core/memory/retrieval.py) |
| **Beta-confident** | `α` / `β` instead of a flat scalar. Confirmations bump α, contradictions bump β. Tie-breaker in retrieval. | [`app/core/memory/confidence.py`](app/core/memory/confidence.py) |
| **Causal DAG** | `derived_from[]` + `contradicts[]`. Answers "why do I believe X?" as a real DAG, not vibes. | [`GET /v1/memory/{id}/explain`](app/api/memory.py) |
| **Skepticism** | Writes are evaluated against priors along contradiction, source-weight, and stakes axes before the chain grows. | [`app/core/memory/skepticism.py`](app/core/memory/skepticism.py) |
| **RRF retrieval** | Five-signal fusion (semantic + lexical + entity + episodic + confidence) with no calibration required. | [`app/core/memory/retrieval.py`](app/core/memory/retrieval.py) |
| **Hash-chained** | Per-user SHA-256 chain. Single-byte tamper is detectable by the `GET /v1/memory/integrity` verifier. | [`app/core/memory/integrity.py`](app/core/memory/integrity.py) |
| **Forgetting** | Per-entity-type decay via `MEMORY_DECAY_PROFILE`. GDPR tombstone via `POST /v1/memory/forget`. No row is ever deleted (chain stays intact). | [`app/core/memory/forgetting.py`](app/core/memory/forgetting.py) |

**Enable it** by setting `MEMORY_ENABLED=true`. It is **off by default** and the regression tripwire (`tests/test_memory_regression.py`) proves that every memory code path is skipped when the flag is off — zero cost to operators who don't want it.

**CLI:**

```bash
nexus memory recall --user alice --entity rex
nexus memory history <belief_id>
nexus memory explain <belief_id>
nexus memory stats
nexus memory verify            # exit 0 = verified, 2 = tampered, 1 = transport error
nexus memory forget <entity>   # GDPR tombstone
```

**Dashboard:** `/dashboard/memory` (overview) and `/dashboard/memory/integrity` (hash-chain verification UI).

Full architecture write-up: **[docs/memory.md](docs/memory.md)**.

---

## OpenClaw / ClawHub integration

OpenClaw skills are static. Nexus makes them **safe** to run. Every imported SKILL.md becomes a `Skill` row that:

- Is re-scored by memory-aware retrieval (skills ranked by `avg_reward`) so the agent naturally prefers ones that work.
- Executes step-by-step through Covernor, so a single bad step (e.g. `shell_exec` against a denied path) halts the whole skill without aborting import.
- Emits a trace per step into the same hash-chained audit log as everything else.
- Auto-disables when its reward slides below a threshold.

**Import a skill (file or URL):**

```bash
# From a URL
curl -X POST http://localhost:9000/v1/skills/import \
  -H "Content-Type: application/json" \
  -d '{"url": "https://clawhub.ai/skills/data-summarizer/SKILL.md"}'

# Or from a local file
curl -X POST http://localhost:9000/v1/skills/import \
  -F "file=@./my-skill.md"

# Via CLI
nexus skills import --from-url https://clawhub.ai/skills/...
nexus skills import ./my-skill.md
```

**Execute a skill (every step passes through Covernor):**

```bash
nexus skills execute <skill_id>
# → emits trace per step, logs Covernor decisions
```

YAML frontmatter provides `name`, `description`, `metadata.openclaw.requires`. The markdown body is heuristically converted to Nexus steps (`tool_call`, `instruction`). `instruction` steps are skipped at execution but injected as LLM context during skill recall. Raw SKILL.md is preserved in `raw_source`; fetch it at `GET /v1/skills/{id}/source`.

`LOCAL_ONLY=true` blocks URL imports (file imports still work).

---

## Hermes-class model integration

Any Hermes-class function-calling model plugs in through the existing provider chain — no special-casing. The `LOCAL_HF_MODEL_ID` setting loads a HuggingFace model locally; the standard provider chain (Gemini, OpenAI, DeepSeek, Ollama) handles everything else.

```bash
# Local Hermes via HuggingFace
LOCAL_HF_MODEL_ID=NousResearch/Hermes-3-Llama-3.1-8B \
LOCAL_HF_DEVICE=cuda \
uvicorn app.main:app --port 9000
```

Or via Ollama (set `OLLAMA_BASE_URL` and use `model_id=ollama:hermes3`):

```bash
curl -X POST http://localhost:9000/v1/agent/run \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Summarize the latest traces", "model_id": "ollama:hermes3"}'
```

What Nexus adds on top of the model:

- **Immune input scan** — the Hermes response cannot be steered by injection payloads the model itself hasn't seen before; the scanner blocks them pre-generation.
- **Covernor on every tool call** — even if the model decides to call `shell_exec`, default-deny kicks in unless a policy explicitly allows it.
- **Critic arbitration** — the critic tree scores the response; a halt-capable critic blocks the release.
- **Hash-chained audit** — every generation, tool call, and belief write becomes an auditable row.
- **Learning loop** — `Episode` + `Belief` + `Skill` reward-tracking means a Hermes deployment actually improves across runs.

Full operator guide — including the plain-JSON tool-call protocol, running Hermes via vLLM / TGI / LiteLLM through the Ollama route, model selection guidance by deployment size, and troubleshooting — lives in [docs/hermes_integration.md](docs/hermes_integration.md).

---

## Digital Employee (agentic mode)

Beyond one-shot generation, Nexus runs a **ReAct-style agent loop** with governed tools, per-step reflection, critic feedback, and task-level reward scoring:

- **Tools** (Covernor-gated): `shell_exec`, `file_read`, `file_write`, `web_fetch`, `search` — see seeded policies in `app/main.py`.
- **API**: `POST /v1/agent/agent/run`, `POST /v1/agent/agent/resume` (after approvals), `POST /v1/agent/agent/feedback`.
- **CLI**: `nexus chat`, `nexus run "…"`, `nexus status`, `nexus approve <id>`, `nexus resume <trace_id>`, `nexus feedback <trace_id> good|bad`, `nexus skills`, `nexus memory …`, `nexus benchmark` (install with `pip install -e .` or use the project venv).
- **Telegram**: set `TELEGRAM_BOT_TOKEN` and start the API; long-polling runs when configured.
- **Memory & training**: episodes and step traces are stored; trajectory export extends the training flywheel (`export_agent_trajectories` in the labeler).
- **Privacy**: set `LOCAL_ONLY=true` to block outbound network (LLM routes to Ollama, tools like `web_fetch`/`search` disabled, Doctrine Lab import skipped, URL-based skill import disabled). Use `model_id=ollama:…` and `OLLAMA_BASE_URL` (default `http://127.0.0.1:11434/v1`). Set `OLLAMA_LIST_IN_PROVIDERS=true` if you want Ollama included in multi-provider discovery; **off by default** so `compare` and auto-routing do not probe localhost unless you opt in.

---

## Configuration

All settings are controlled via environment variables (or a `.env` file). Copy `.env.example` to `.env` and edit.

### AI Providers

Set at least one to use real LLM generation. Without any keys, Nexus runs in mock mode.

| Variable | Description | Default |
|----------|-------------|---------|
| `GEMINI_API_KEY` | Google Gemini API key | (empty) |
| `GEMINI_MODEL` | Gemini model name | `gemini-2.5-flash` |
| `OPENAI_API_KEY` | OpenAI API key | (empty) |
| `OPENAI_MODEL` | OpenAI model name | `gpt-4o-mini` |
| `DEEPSEEK_API_KEY` | DeepSeek API key | (empty) |
| `DEEPSEEK_MODEL` | DeepSeek model name | `deepseek-chat` |
| `OLLAMA_BASE_URL` | OpenAI-compatible Ollama API base URL | `http://127.0.0.1:11434/v1` |
| `OLLAMA_DEFAULT_MODEL` | Model name when using `ollama:` routes | `llama3.2` |
| `OLLAMA_LIST_IN_PROVIDERS` | Include Ollama in provider auto-discovery | `false` |
| `LOCAL_HF_MODEL_ID` | HuggingFace model ID for local inference (Hermes etc.) | (empty) |
| `LOCAL_HF_DEVICE` | Device for local HF model (`cpu`, `cuda`, `mps`) | `cpu` |
| `LOCAL_ONLY` | Enforce no outbound HTTP | `false` |

Provider selection: if you set `model_id` in your request, it routes by prefix (`gpt` → OpenAI, `gemini` → Gemini, `deepseek` → DeepSeek, `ollama:` → Ollama). Without an explicit `model_id`, the default chain is Gemini → OpenAI → DeepSeek → mock.

### Memory

| Variable | Description | Default |
|----------|-------------|---------|
| `MEMORY_ENABLED` | Master switch. When false, zero memory code paths execute. | `false` |
| `EXTRACTION_MODEL` | Model ID for the belief extractor. Empty ⇒ fall back to main model. | (empty) |
| `MEMORY_STAKES_THRESHOLDS` | Skepticism cutoffs per entity type. | `identity=0.9,financial=0.85,preference=0.5,state=0.3` |
| `MEMORY_DECAY_PROFILE` | Per-entity-type TTL for background forgetting. | `identity=inf,preference=180d,state=4h,context=1h` |
| `MEMORY_RETRIEVAL_LIMIT` | Default top-k for RRF retrieval. | `5` |
| `MEMORY_EXTRACTOR_MAX_CHARS` | Budget cap on prompts fed to the extractor. | `8000` |

See [docs/memory.md](docs/memory.md) for the full architecture.

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

All API endpoints are served under both **`/v1/*`** (canonical) and **`/api/*`** (legacy backward-compatible). Legacy routes carry `Deprecation: true` + `Link: </v1/…>; rel="successor-version"`. Prefer `/v1/*` in new code.

All endpoints require `X-API-Key` header when `NEXUS_API_KEY` is set. Interactive docs at **http://localhost:9000/docs** (Swagger) or **/redoc**.

### Agent Execution

#### `POST /v1/agent/run` — Run the pipeline

```bash
curl -X POST http://localhost:9000/v1/agent/run \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-key" \
  -d '{
    "prompt": "Explain neural networks",
    "session_id": "optional-session-id",
    "model_id": "gemini-2.5-flash"
  }'
```

- `session_id` — groups related prompts for escalation tracking and hash chaining
- `model_id` — override the LLM provider (`gpt-4o-mini`, `gemini-2.5-flash`, `deepseek-chat`, `ollama:hermes3`, etc.)

Supports `Idempotency-Key` header for safe retries.

#### `POST /v1/agent/stream` — Stream tokens via SSE

Same request body as `/run`. Returns Server-Sent Events:

```bash
curl -N -X POST http://localhost:9000/v1/agent/stream \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Write a haiku about AI"}'
```

Events:
- `event: status` — pipeline stage (`input_scan`, `generating`, `evaluating`)
- `event: token` — `{"text": "...", "index": 0}`
- `event: done` — `{"trace_id": "...", "status": "completed", "latency_ms": 1234}`
- `event: error` — `{"status": "blocked", "error": "..."}`

#### `POST /v1/agent/compare` — Multi-model comparison

Run the same prompt through multiple models, score each with the critic tree, and pick the best:

```bash
curl -X POST http://localhost:9000/v1/agent/compare \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Explain quantum entanglement",
    "model_ids": ["gemini-2.5-flash", "gpt-4o-mini"]
  }'
```

Returns all candidates with scores, plus the winner. If `model_ids` is omitted, all configured providers are used (Ollama only when `OLLAMA_LIST_IN_PROVIDERS=true`).

#### `POST /v1/agent/agent/run` — Agentic loop (tools + reflection)

Runs `run_agent()` with the same immune scan and governance as the core pipeline. Request body: `prompt`, optional `session_id`, `model_id`, `max_steps`, `resume_state`.

#### `POST /v1/agent/agent/resume` — Resume after approval

Continues an agent trace that paused on `require_approval` once votes/token are satisfied.

#### `POST /v1/agent/agent/feedback` — Task feedback

Attach `good` / `bad` user feedback to a completed trace (feeds reward scoring and episodic memory).

### Memory

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/memory` | List live beliefs (`?user=alice&entity=rex&predicate=age&limit=50`) |
| `GET` | `/v1/memory/{id}/history` | Bitemporal history of a single belief |
| `GET` | `/v1/memory/{id}/explain` | Derivation DAG (JSON + mermaid) |
| `POST` | `/v1/memory/forget` | GDPR tombstone (never deletes; chain stays intact) |
| `GET` | `/v1/memory/stats` | Counts + confidence distribution |
| `GET` | `/v1/memory/integrity` | Hash-chain verifier (200 + `verified=false` on tamper) |

All memory endpoints are Covernor-gated. Full contract in [docs/memory.md](docs/memory.md).

### Skills (OpenClaw import + reusable workflows)

High-reward agent episodes are auto-abstracted into reusable skills. Additionally, SKILL.md payloads (OpenClaw / ClawHub format) can be imported via `/v1/skills/import`. Every execution step passes through Covernor.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/skills/import` | Import SKILL.md by file upload or URL |
| `GET` | `/v1/skills` | List skills (`?enabled_only=true`) |
| `GET` | `/v1/skills/{id}` | Full skill detail (steps, reward stats, hash) |
| `GET` | `/v1/skills/{id}/source` | Raw SKILL.md source (if imported) |
| `POST` | `/v1/skills/{id}/execute` | Execute a skill step-by-step with Covernor gating |
| `DELETE` | `/v1/skills/{id}` | Permanently delete a skill |

### Traces (Audit Log)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/traces` | List traces (`?session_id=`, `?status=`, `?limit=`, `?offset=`) |
| `GET` | `/v1/traces/{id}` | Full trace details |
| `GET` | `/v1/traces/{id}/replay` | Step-by-step pipeline replay |
| `POST` | `/v1/traces/{id}/re-evaluate` | Re-run current critic tree on a stored trace |
| `GET` | `/v1/traces/session/{session_id}/verify-chain` | Verify hash chain integrity |
| `GET` | `/v1/traces/audit/export` | SIEM-compatible NDJSON audit export |

### Governance

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/governance/policies` | List governance policies |
| `POST` | `/v1/governance/policies` | Create a new policy |
| `GET` | `/v1/governance/approvals` | List pending approval requests |
| `POST` | `/v1/governance/approve/{request_id}` | Submit an approval/denial vote |

Policies control what the agent is allowed to do. The system is **default-deny** — if no policy matches, the action is blocked. Policy decisions: `allow`, `deny`, `require_approval`. When a policy requires approval, the pipeline creates an `ApprovalRequest` and returns `status: "pending_approval"`; once K approvers vote yes, an ECDSA capability token is minted and the trace is released.

### Critic Registry

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/critic/registry` | List registered critic nodes |
| `POST` | `/v1/critic/registry` | Register or update a critic node |
| `PATCH` | `/v1/critic/registry/{name}` | Update a critic node |

Critic nodes are hot-swappable — add, update, or deactivate them at runtime without restarting the server.

### Training Flywheel

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/training/queue` | View labeling queue |
| `POST` | `/v1/training/queue/{item_id}/label` | Apply a human label to a queued item |
| `POST` | `/v1/training/export` | Export labeled items as training data |
| `POST` | `/v1/training/eval` | Submit eval report to Doctrine Lab |
| `POST` | `/v1/training/finetune` | Trigger fine-tuning via Doctrine Lab |
| `POST` | `/v1/training/promote-adapter` | Promote a LoRA adapter to active critic |
| `POST` | `/v1/training/lora/compare` | Compare critic before/after LoRA swap |

### Health & Metrics

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness probe |
| `GET` | `/health/ready` | Readiness probe (DB, circuit breakers, pool stats); `?deep=true` probes providers |
| `GET` | `/metrics` | Prometheus metrics (opt-in via `EXPOSE_METRICS=true`) |
| `GET` | `/v1/agent/version` | Build metadata (api_version, project_version, python_version, git_sha) |

---

## Dashboard

A browser-based UI at **http://localhost:9000/dashboard** with sections for:

- **Trace Explorer** — browse all pipeline traces with status counts. Click for full details (prompt, response, every step, critic scores, governance decisions).
- **Memory** — overview of live/tombstoned beliefs and hash chains; separate page for integrity verification with a CSRF-protected verify form.
- **Labeling Queue** — review failure traces queued for training. Apply labels (`correct_flag`, `incorrect`, `false_positive`, `needs_review`).
- **Approval Console** — pending approval requests with approve/deny buttons.
- **Calibration Dashboard** — Expected Calibration Error (ECE) across critic nodes; accuracy-vs-confidence bins.
- **Circuit Breakers** — per-provider state, failure meter, manual reset.
- **Provider Health** — unified config + CB + live-probe view of all configured LLM providers.
- **Skills Library** — view all skills (auto-generated + imported), enable/disable, execute, or import a new SKILL.md.

When `NEXUS_API_KEY` is set, the dashboard requires login. In development mode (no API key), it is open.

---

## Deployment

### Docker (SQLite)

```bash
cp .env.example .env
make docker-up
# or: docker compose up --build -d
```

### Docker (PostgreSQL + Redis)

```bash
docker compose --profile prod up --build -d
```

Starts Postgres 16, Redis 7, and Nexus with gunicorn + 2 uvicorn workers under resource limits.

### Production Checklist

1. **`ENVIRONMENT=production`** — enables JSON structured logging and startup checks
2. **`NEXUS_API_KEY`** — required; app refuses to start without it in production
3. **`SESSION_SECRET`** — required for dashboard session security
4. **`ENFORCE_DASHBOARD_CSRF=true`** — CSRF protection for dashboard forms
5. **PostgreSQL** — SQLite is for development only
6. **Retention** — set `RETENTION_*_DAYS` to prevent unbounded data growth
7. **Metrics** — set `EXPOSE_METRICS=true` and scrape `/metrics` with Prometheus
8. **OTel** — set `OTEL_ENABLED=true` and `OTEL_EXPORTER_ENDPOINT` for distributed tracing

### Multi-Worker Deployment

Rate limiting, capability tokens, and the training scheduler default to in-process state. For `--workers > 1`:

- Set `NEXUS_SKIP_SCHEDULER=1` on all workers except one (prevents duplicate background jobs)
- Set `REDIS_URL` for shared rate limiting + idempotency across workers
- Capability tokens are verified in the issuing process; for cross-worker verification, persist tokens in the DB

---

## Development

### Common Commands

```bash
make dev          # Install deps, migrate, start with hot-reload
make test         # Run all 1400 tests
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

# One of the benchmark exit-gates as pytest
pytest tests/eval/temporal_qa.py -v

# Or as CLI (emits JSON for CI)
python -m tests.eval.temporal_qa --json
```

### Project Structure

| Directory | Purpose |
|-----------|---------|
| `app/agent/` | Pipeline orchestrator — the core of the system |
| `app/api/` | FastAPI route handlers (`/v1/*` canonical, `/api/*` legacy) |
| `app/core/immune/` | Injection detection (11 languages), memory bank, prompt hardening |
| `app/core/asflc/` | A-S-FLC decision framework — asymmetric risk evaluation |
| `app/core/llm/` | LLM provider abstraction (Gemini, OpenAI, DeepSeek, Ollama, local HF, mock) |
| `app/core/covernor/` | Governance engine — policies, approvals, ECDSA tokens |
| `app/core/critic/` | Arbiter critic tree — pluggable evaluation nodes |
| `app/core/memory/` | Beliefs, retrieval (RRF), skepticism, writer, integrity, forgetting |
| `app/core/mcp/` | Governed MCP proxy for remote tool backends |
| `app/core/training/` | Training flywheel — labeling, calibration, export, scheduling |
| `app/services/` | Cross-cutting services — integrity, replay, retention, Doctrine Lab bridge |
| `app/models/` | SQLAlchemy models |
| `app/templates/` | Jinja2 templates for the dashboard |
| `tests/` | 1400 tests across 59 unit files + 6 benchmark eval files |
| `docs/` | [memory.md](docs/memory.md), [benchmarks.md](docs/benchmarks.md) |

---

## Architecture Deep Dive

### Core Components

**Agent-Immune Scanner** (`app/core/immune/scanner.py`)
Detects prompt injection attacks across 11 languages (English, Chinese, Russian, Arabic, Hindi, Japanese, Korean, Spanish, French, German, Portuguese). Uses a Semantic Memory Bank to remember previously blocked attacks and fuzzy-match new ones. Session escalation tracks repeated suspicious prompts. Flagged prompts are hardened (injection fragments stripped) before LLM generation. 145 adversarial tests in `test_redteam.py`.

**A-S-FLC Decision Engine** (`app/core/asflc/`)
Asymmetric-Subjective Fuzzy Logic Controller. For complex prompts, uses the LLM to decompose the request into alternative decision paths, evaluates each with asymmetric risk treatment (negative outcomes are penalized harder), and produces a system hint that guides the main generation. Skipped for short/simple prompts.

**Arbiter Critic Tree** (`app/core/critic/`)
Evaluates every LLM response through pluggable critic nodes loaded from the database. Each node (Reasoning, Injection, Safety, Quality) scores the response. Nodes can be added, updated, or deactivated at runtime. If any halt-capable node triggers, the response is blocked and the failure is queued for human review.

**Covernor Governance** (`app/core/covernor/`)
Default-deny policy engine. Every action must be explicitly allowed by a policy. High-risk actions require K-of-N human approval — once quorum is reached, an ECDSA capability token is cryptographically minted as proof of authorization, single-use, verified on release.

**Memory** (`app/core/memory/`)
Bitemporal belief store with Beta-distributed confidence, causal derivation DAG, and per-user hash chain. Reads via RRF fusion over 5 signals; writes pass through extractor → immune scan → skepticism → Covernor → chain append. See [docs/memory.md](docs/memory.md).

**MCP Governance Proxy** (`app/core/mcp/`)
When `MCP_ENABLED=true`, Nexus mounts a FastMCP server at `/mcp` and exposes all registered backend tools as governed proxies. Each `tools/call` runs immune scan → Covernor policy check → forward → trace, using namespaced `mcp:{backend}:{tool}` policies.

**Training Flywheel** (`app/core/training/`)
Every failure (critic halt, governance denial, immune block, memory denial) is pushed to a labeling queue. Human reviewers assign labels via the dashboard or API. Labeled items are exported as training data, optionally enriched with evidential uncertainty metadata, and sent to Doctrine Lab for fine-tuning. A background scheduler handles automated exports and calibration snapshots.

### Hash Chain Integrity

Every trace is linked to the previous trace in its session via `prev_hash` → `trace_hash`, forming a tamper-evident chain. Beliefs have their own per-user chain. Verify:

```bash
# Trace chain
curl http://localhost:9000/v1/traces/session/YOUR_SESSION_ID/verify-chain

# Memory chain (per-user or full-store audit)
curl "http://localhost:9000/v1/memory/integrity?user=alice"
nexus memory verify
```

### Sister Project: Doctrine Lab

[Doctrine Lab](https://github.com/denial-web/doctrine-lab) is the training factory that complements Nexus:
- Nexus generates failure traces → exports them as training data
- Doctrine Lab curates datasets, runs fine-tuning, and evaluates models
- Fine-tuned LoRA adapters are hot-swapped back into Nexus's critic nodes

---

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines. For security vulnerabilities, see [SECURITY.md](SECURITY.md).

---

## Trademarks

OpenClaw and ClawHub are trademarks of their respective owners.
Hermes and Nous Research are trademarks of Nous Research, Inc.

Nexus Agent is an independent open-source project and is not
affiliated with, endorsed by, or sponsored by these projects.
References to OpenClaw skills and Hermes-class models in the
documentation, code, and launch materials describe interoperability
only.

---

## License

Licensed under the [Apache License 2.0](LICENSE).
