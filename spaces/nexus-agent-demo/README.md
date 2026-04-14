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
short_description: Zero-trust security pipeline for LLM agents
tags:
  - llm
  - ai-safety
  - prompt-injection
  - security
  - zero-trust
---

# Nexus Agent — Zero-Trust Pipeline Demo

Interactive demo of the [Nexus Agent](https://github.com/denial-web/nexus-agent) security pipeline.

Every prompt passes through 7 security checkpoints:
1. **Input scan** — 11-language injection detection
2. **A-S-FLC** — asymmetric risk decision analysis
3. **LLM generation** — mock mode (no API keys needed)
4. **Critic evaluation** — Reasoning, Injection, Safety, Quality nodes
5. **Governance** — default-deny policy engine
6. **Output scan** — blocks leaked secrets
7. **Hash-chained trace** — tamper-evident audit log

Try injection attacks in English, Chinese, Russian, Spanish, Arabic, French, or German — watch them get blocked or hardened in real time.
