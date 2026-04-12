# Nexus Agent

Zero-Trust & Self-Evolving AI Agent System.

A production-grade agent runtime that merges a **Zero-Trust Agent Pipeline** with a **Self-Improving, Risk-Aware Custom LLM**. Every prompt is scanned, every decision is uncertainty-checked, every tool call requires governance approval, and every failure feeds back into model improvement.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    GATEWAY LAYER                        │
│  Agent-Immune (input scan) → Covernor (output firewall) │
├─────────────────────────────────────────────────────────┤
│                     BRAIN LAYER                         │
│  A-S-FLC (decision engine) ← Arbiter (critic tree)     │
├─────────────────────────────────────────────────────────┤
│                   FLYWHEEL LAYER                        │
│  Failure traces → Labeling queue → Fine-tune → Deploy   │
└─────────────────────────────────────────────────────────┘
```

### Core Components

| Module | Purpose |
|--------|---------|
| `core/immune/` | Agent-Immune gateway — semantic prompt injection detection |
| `core/asflc/` | A-S-FLC decision framework — asymmetric risk-penalized reasoning |
| `core/covernor/` | Governance — default-deny policy, ECDSA tokens, K-of-N approval |
| `core/critic/` | GrokForge Arbiter — critic tree with Safety/Reasoning/Injection leaf nodes |
| `core/training/` | Flywheel — failure labeling, LoRA hot-swap, continuous fine-tuning |

## Quick Start

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your API keys

# Run database migrations
alembic upgrade head

# Start the server
uvicorn app.main:app --reload --port 9000
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness probe |
| `POST` | `/api/agent/run` | Execute agent pipeline on a prompt |
| `GET` | `/api/traces` | List execution traces |
| `GET` | `/api/traces/{id}` | Replay a specific trace |
| `GET` | `/api/critic/registry` | List registered critic nodes |
| `POST` | `/api/critic/registry` | Register/update a critic node |
| `GET` | `/api/governance/policies` | List active policies |
| `POST` | `/api/governance/approve/{trace_id}` | Submit approval for a pending action |
| `GET` | `/api/training/queue` | View labeling queue |

## Build Phases

1. **Foundation** — DB models, critic registry, base schemas ✅
2. **Live Critic Layer** — Arbiter + chunked generate-then-verify loop
3. **First Leaf Nodes** — Reasoning Critic + Injection Critic
4. **Governance Layer** — Covernor policy engine + ECDSA tokens
5. **Full Pipeline** — End-to-end agent runtime
6. **Flywheel** — Automated failure capture → fine-tuning loop

## Connection to Doctrine Lab

This project consumes datasets and fine-tuned models from [Doctrine Lab](../thinking-DT/doctrine-lab/). Doctrine Lab provides the training data factory and proof engine; Nexus Agent provides the runtime and generates failure traces that cycle back as training signal.
