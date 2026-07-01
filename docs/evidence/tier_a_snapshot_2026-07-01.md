# Tier A evidence snapshot — 2026-07-01

Frozen public copy of champion evidence for launch. Regenerate from the eval factory with `make champion-evidence` (maintainers).

**Champion:** `local-lora:injection-mixed-safety-v8-3b`  
**Portfolio gate:** PASS · **Integration smoke:** PASS

---

## OOD injection holdout (v8, internal gate — do not cite as international score)

- **hard:** FNR=0.0 FPR=0.0 cap_miss=0.0833
- **framed:** FNR=0.0 FPR=0.0 cap_miss=0.0

---

## InjecAgent external subset (48 attacks)

| Model | FNR ↓ | FPR ↓ | Accuracy |
|-------|-------|-------|----------|
| v8 champion | 0.0208 | 0.0 | 0.9848 |
| gpt-4o (anchor) | 0.0208 | 0.0 | 0.9848 |
| deepseek-chat (current) | 0.0208 | 0.0 | 0.9848 |
| nexus:local (defended stack) | 0.0208 | 0.0 | 0.9848 |

Head-to-head: v8 **ties** gpt-4o, deepseek-chat, and nexus:local on FNR.

---

## AgentDojo workspace subset5 (6 tasks)

Fixed spec: 5 user tasks + `injection_task_0`, `agentdojo==0.1.35`.

| Model | Utility | Security |
|-------|---------|----------|
| gpt-4o (anchor) | 0.2 | 1.0 |
| gpt-4o-mini (current) | 0.2 | 1.0 |
| v8 Ollama | 0.0 | 0.0 |
| nexus:local (defended shim) | 0.0 | 0.6 |

---

## Agent regression vs baseline (internal gate)

| Category | pattern_pass |
|----------|--------------|
| agent_safety | 1.0 |
| injection_resistance | 1.0 |
| agent_governance | 1.0 |
| agent_reasoning | 1.0 |

Regressed tasks: **0**
