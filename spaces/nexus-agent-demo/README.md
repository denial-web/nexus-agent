---
title: Nexus Agent Demo
emoji: 🛡️
colorFrom: blue
colorTo: indigo
sdk: gradio
sdk_version: 5.29.0
app_file: app.py
pinned: false
license: apache-2.0
short_description: A governed AI agent with built-in audit and learning.
tags:
  - llm
  - ai-safety
  - prompt-injection
  - security
  - zero-trust
---

# Nexus Agent — Zero-Trust Pipeline Demo

Interactive demo of the [Nexus Agent](https://github.com/denial-web/nexus-agent) — a full agent system with governance, memory, and audit built in.

**What Nexus is:** a complete agent out of the box — tools (shell, files, web, search), a ReAct loop with reflection and self-correction, auto-generated skills from high-reward trajectories, bitemporal memory, and multi-provider LLM routing (Gemini / OpenAI / DeepSeek / Ollama / local HF). Every call — LLM, tool, memory write — passes through the same zero-trust pipeline.

## What this demo shows

Nine preset prompts, each triggering a **distinct behaviour** in the seven-step pipeline. Click through them in order and you will have seen every pipeline step actually *do* something, not just log a placeholder:

| Preset | Expected outcome | Demonstrates |
|---|---|---|
| ✅ Safe (1) — quantum computing | `COMPLETED` | Clean path, all 7 steps green |
| ✅ Safe (2) — encryption | `COMPLETED` | Technical content passes critic tree |
| 🛑 Injection (EN) — DAN jailbreak | `BLOCKED` | Step 1 English injection scanner |
| 🛑 Injection (中文) | `BLOCKED` | Step 1 Chinese patterns — proves multi-lang |
| 🛑 Injection (Русский) | `BLOCKED` | Step 1 Cyrillic patterns |
| 🟡 Hardening — partial injection | `COMPLETED` (hardened) | Injection fragment stripped, clean portion passes |
| ⚠️ Critic halt — unsafe content | `HALTED` | Step 4 safety critic fails on model output |
| 🛑 Output leak — API keys in response | `OUTPUT_BLOCKED` | Step 6 output scanner catches leaked secrets |
| 🛑 Governance deny — destructive shell | `DENIED` | Step 5 Covernor default-deny policy fires |

## The hash-chain verifier (scroll to the bottom)

Every trace you run is appended to an in-memory SHA-256 chain using the **same contract** as `app/services/integrity.py` in production. The Audit Chain panel has three buttons:

- **Verify Chain** — recomputes every hash and shows ✅ per row
- **Simulate Tamper** — modifies a past trace's prompt AND updates its stored hash (the full database-level attacker model)
- **Verify Chain (again)** — shows the cascade: rows upstream of the tamper stay ✅, but every row from the tamper onwards goes 🛑, because `prev_hash` references the original hash and integrity cannot be re-established

This is the single most convincing proof of the tamper-evident audit claim. Try it.

## What this demo does not show

- Full agent loop (planning, tools, reflection, skill recall) — needs live LLM
- Bitemporal memory + causal graph — needs database
- OpenClaw SKILL.md import or Hermes model routing — needs API keys

All of those are exercised in the repo's [1,401 tests](https://github.com/denial-web/nexus-agent) and the six CI-gated [nightly benchmarks](https://github.com/denial-web/nexus-agent/blob/main/docs/benchmarks.md).

## Source

- Demo: [`spaces/nexus-agent-demo/app.py`](https://github.com/denial-web/nexus-agent/blob/main/spaces/nexus-agent-demo/app.py)
- Production pipeline: [`app/agent/pipeline.py`](https://github.com/denial-web/nexus-agent/blob/main/app/agent/pipeline.py)
- Hash chain: [`app/services/integrity.py`](https://github.com/denial-web/nexus-agent/blob/main/app/services/integrity.py)
