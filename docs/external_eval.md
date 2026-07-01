# External evaluation — Tier A benchmarks

**Audience:** HN, Reddit, partners, security reviewers  
**Nightly CI benchmarks (in-repo):** [benchmarks.md](benchmarks.md)  
**Evidence snapshot:** [evidence/tier_a_snapshot_2026-07-01.md](evidence/tier_a_snapshot_2026-07-01.md)  
**Last refresh:** 2026-07-01

---

## One-line pitch

**Open-source defended agent runtimes (Nexus Agent, ClawGuard) with reproducible Tier A injection benchmarks — not a general-purpose frontier model.**

---

## Three-row story (what to show publicly)

| Row | What it is | InjecAgent subset FNR ↓ | AgentDojo subset5 security |
|-----|------------|-------------------------|----------------------------|
| **Undefended API** | gpt-4o-mini (current frontier baseline) | 0.0208 (same subset) | **1.0** |
| **Safety specialist** | v8 critic (`injection-mixed-safety-v8-3b`, 3B LoRA via Ollama) | **0.0208** (ties anchor) | 0.0 |
| **Defended stack** | Full Nexus pipeline (`nexus:local`) | **0.0208** | **0.6** |

**How to read this:** API models win on agent *utility*; the defended stack is the product story — immune scan + governance + critic + audit. v8 is the **certified critic layer**, not a ChatGPT replacement.

Utility on subset5: gpt-4o / gpt-4o-mini **0.2**; v8 and Nexus **0.0** — do not headline utility for the 3B specialist.

---

## Tier A benchmarks (citeable)

### InjecAgent derivative subset

| Item | Value |
|------|--------|
| Paper | Zhan et al., [arXiv:2403.02691](https://arxiv.org/abs/2403.02691) |
| Cases | 48 attacks / 18 benign (deterministic derivative — **not** full upstream 1,054) |
| Metric | FNR = attack success rate (attacker tool invoked) |

### AgentDojo official harness (fixed derivative)

| Item | Value |
|------|--------|
| Paper | Debenedetti et al., NeurIPS 2024 D&B, [arXiv:2406.13352](https://arxiv.org/abs/2406.13352) |
| Harness | Upstream `agentdojo==0.1.35` — we do not reimplement scoring |
| Leaderboard | https://agentdojo.spylab.ai/results/ |

**Disclosure:** smoke + subset5 only — **not** a full AgentDojo leaderboard claim.

---

## What we do **not** claim

- Full InjecAgent upstream leaderboard without running their full harness
- Full AgentDojo leaderboard from a 5-task subset
- "Beat GPT overall" — we tie on attack FNR on our fixed InjecAgent subset; APIs lead on utility
- OWASP certification — design alignment only
- MMLU / Chatbot Arena leadership for a 3B safety specialist

---

## Reproduce locally

### Nexus injection demo (no provider API keys)

```bash
git clone https://github.com/denial-web/nexus-agent.git
cd nexus-agent && make dev
# Fast immune-only scan (~1s, no LLM calls):
python examples/injection_demo.py --immune-only
# Full pipeline (mock LLM when no provider keys in .env):
python examples/injection_demo.py
```

### Nexus nightly benchmarks (in this repo)

```bash
pytest tests/eval/ -q
python -m tests.eval.tool_injection_redteam --json
```

See [benchmarks.md](benchmarks.md) for exit gates.

### Tier A external refresh (eval factory)

Full InjecAgent + AgentDojo subset reproduction runs in the private **Doctrine Lab** eval factory (`make eval-external-baselines`, `make agentdojo-benchmark-nexus-subset5`). Committed numbers live in [evidence/tier_a_snapshot_2026-07-01.md](evidence/tier_a_snapshot_2026-07-01.md). Partner access: contact maintainers for reproduce bundle.

---

## Sister repos

| Repo | License | Role |
|------|---------|------|
| [nexus-agent](https://github.com/denial-web/nexus-agent) | Apache 2.0 | Flagship defended runtime |
| [ClawGuard](https://github.com/denial-web/clawguard) | MIT | Lighter runtime / skill import |

---

## Launch copy

Draft posts: [LAUNCH_POSTS.md](../LAUNCH_POSTS.md)
