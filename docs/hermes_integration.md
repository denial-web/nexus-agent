# Nexus Agent — Hermes-class Model Integration

> **One-liner.** Any function-calling LLM — Hermes, Llama-3-Instruct,
> Qwen2.5-Coder, Mistral-Nemo, whatever is current — plugs into Nexus
> through the existing provider chain. Nexus uses a plain JSON tool
> protocol (not OpenAI native function calling), so any model that can
> produce well-formed JSON is compatible out of the box. The runtime
> adds what the model doesn't have: immune-scanned prompts,
> Covernor-gated tool calls, critic-tree arbitration, hash-chained
> traces, and an Episode/Belief/Skill learning loop.

This doc is the operator guide for running a Hermes-class model
inside the Nexus runtime. For architecture, see
[AGENTS.md](../AGENTS.md) §"Digital employee" and §"LLM provider chain".
For the OpenClaw companion doc, see
[docs/openclaw_integration.md](openclaw_integration.md).

---

## Contents

1. [What "Hermes-class" means here](#what-hermes-class-means-here)
2. [What Nexus adds on top of the model](#what-nexus-adds-on-top-of-the-model)
3. [The tool-call protocol — plain JSON, not OpenAI tools API](#the-tool-call-protocol--plain-json-not-openai-tools-api)
4. [Three ways to run a Hermes model](#three-ways-to-run-a-hermes-model)
5. [Registering tools the model can call](#registering-tools-the-model-can-call)
6. [End-to-end walkthrough](#end-to-end-walkthrough)
7. [Model selection guidance](#model-selection-guidance)
8. [Performance & tuning](#performance--tuning)
9. [Troubleshooting](#troubleshooting)

---

## What "Hermes-class" means here

For the purposes of this guide, a "Hermes-class" model is any
open-weight LLM that is:

- **Instruction-following** — obeys a system prompt.
- **Structured-output-reliable** — emits valid JSON on demand
  without wrapping it in prose or fences.
- **Tool-calling-trained** — has seen enough tool-call-shaped
  training data to pick a tool and populate its arguments.

Nexus has been exercised against [Nous Research's Hermes
series](https://huggingface.co/NousResearch) (the name sticks for
historical reasons), but everything in this guide applies to any
model hitting those three bars — Llama-3.1-Instruct, Qwen2.5-Instruct,
Mistral-Nemo-Instruct, DeepSeek-V2.5, GPT-OSS if it ships, etc.

What Nexus explicitly does **not** require:

- Native OpenAI `tools=[…]` support. We parse JSON from text (see below).
- A specific chat template. Any chat-tuned model works; we don't special-case.
- A specific context length. Longer is better for skill-recall, but 8k is fine.

---

## What Nexus adds on top of the model

A Hermes-class model on its own gives you *generation* and *tool
selection*. It does not give you any of the six things below. Running
the same model through Nexus adds them:

| Concern | Hermes alone | Hermes via Nexus |
|---|---|---|
| Prompt injection from the user | Model sees it directly | Immune scanner blocks or hardens 11-language injection in `app/core/immune/scanner.py` |
| Malicious tool-call emitted by the model | Executed | Covernor `evaluate_action` runs before the tool fires; default-deny |
| Hallucinated/low-quality tool calls | Executed anyway | Arbiter critic tree scores the response; halt-capable critics block release |
| "Why did the agent do X?" | Raw model transcript | Hash-chained trace per step: prompt → tool call → result → critic → governance; verifiable via `/v1/traces/*/verify-chain` |
| Memory of past runs | None | `Episode` / `Belief` / `Skill` tables; skill-recall injects proven workflows into the system prompt (see [docs/memory.md](memory.md)) |
| Failure data for fine-tuning | Lost | Every critic halt / governance deny lands in the labeling queue, exports to [Doctrine Lab](https://github.com/denial-web/doctrine-lab) |

These are additive, not alternative. Nothing in the list changes the
model's weights or its API surface to the developer; they sit *around*
the model.

---

## The tool-call protocol — plain JSON, not OpenAI tools API

This is the single most important thing to understand when wiring a
Hermes-class model into Nexus.

Nexus does **not** forward OpenAI-style `tools=[…]` parameters to the
provider. The agent loop puts the tool registry into the system prompt
as plain JSON, asks the model to return one of two JSON shapes, and
parses the model's text output:

### The two shapes the model must return

```json
{"action":"tool_call","tool":"<tool-name>","arguments":{...}}
```

```json
{"action":"final_answer","content":"<plain text or markdown>"}
```

That's the entire contract. Source:
[`app/agent/agent_loop.py`](../app/agent/agent_loop.py) — grep for
`"You are a governed agent"`.

### Why plain JSON, not OpenAI function-calling

- **Provider-portable.** Same system prompt works on Gemini, OpenAI,
  DeepSeek, Ollama, vLLM, TGI, a local HuggingFace model, or the
  mock provider. No per-provider tool-schema translator to maintain.
- **Model-portable.** Any model that reliably emits JSON works —
  which is most instruction-tuned models today, Hermes being the most
  famous one trained explicitly for it.
- **Governance-aligned.** Covernor inspects the *parsed* tool call
  after the model emits it; there's no second path where the provider
  executes a tool before we get a chance to veto.

### The system prompt the model actually sees

```
You are a governed agent. Reply with a single JSON object only, no markdown, either:
{"action":"tool_call","tool":"<name>","arguments":{...}}
or {"action":"final_answer","content":"<markdown or plain text summary>"}
Available tools:
{<tool registry as JSON>}
Obey workspace safety: use paths under the working directory unless policy allows otherwise.

[Reusable skills (follow these proven steps if they match your task):
<retrieved skills, if any, ranked by avg_reward>]

[Past experience (use to guide your plan):
<retrieved episodes>]

[Known beliefs about the user (high-confidence, current):
<retrieved beliefs>]
```

Hermes models trained on JSON-tool-calling data produce the right shape
reliably on the first try. For a weaker model, the `run_agent` loop
will retry parsing up to `max_steps` and escalate to `final_answer` if
JSON never materialises — a pragmatic fallback that avoids wedging the
loop on a model that can't cooperate.

---

## Three ways to run a Hermes model

Pick whichever matches your infrastructure. All three end up in the
same `_resolve_route` function at
[`app/core/llm/provider.py`](../app/core/llm/provider.py) and look
identical to the rest of the pipeline.

### Option A — Local HuggingFace (`LOCAL_HF_MODEL_ID`)

In-process inference via `transformers` + `torch`. Best for a
single-user, single-GPU workstation or a small internal dev box.

```bash
# .env
LOCAL_HF_MODEL_ID=NousResearch/Hermes-3-Llama-3.1-8B
LOCAL_HF_DEVICE=cuda        # or "cpu", "mps"

# Request
curl -X POST http://localhost:9000/v1/agent/run \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "List the files in the current directory.",
    "model_id": "local"
  }'
```

The first request warm-loads the tokenizer and model; subsequent
requests reuse the in-process singleton (see `_get_local_model` and
`_get_local_tokenizer`). If `transformers`/`torch` are not installed,
the provider emits a deterministic stub string so the pipeline still
exercises end-to-end in CI — this is why our test suite doesn't need a
GPU. Production deployments will want to install the GPU extras.

Generation parameters are currently fixed at `max_new_tokens=256,
do_sample=False` for determinism; sampling configurability is a
Phase 13 item.

### Option B — Ollama (`model_id=ollama:…`)

Ollama ships a serviceable OpenAI-compatible HTTP server and handles
model pulling, quantization, and lifecycle. Best for a dev-team-level
shared box, or a single-operator laptop that doesn't want to manage
`transformers` directly.

```bash
# Host: run Ollama separately
ollama pull hermes3
# Ollama now serves at http://127.0.0.1:11434/v1

# .env
OLLAMA_BASE_URL=http://127.0.0.1:11434/v1
OLLAMA_DEFAULT_MODEL=hermes3

# Request
curl -X POST http://localhost:9000/v1/agent/run \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "List the files in the current directory.",
    "model_id": "ollama:hermes3"
  }'
```

`OLLAMA_LIST_IN_PROVIDERS=true` additionally makes Ollama show up in
`/v1/agent/compare` auto-discovery and `get_available_providers()`. It
defaults to off so that neither `compare` nor auto-routing silently
probe localhost unless you opt in.

### Option C — Any OpenAI-compatible server (vLLM / TGI / LiteLLM / custom)

Production-grade serving frameworks — vLLM, Text Generation Inference
(TGI), or a LiteLLM gateway — speak OpenAI's chat completions API.
Nexus reuses the Ollama route for this because the Ollama client is
really "the OpenAI client pointed at a local base URL." Just change
the base URL:

```bash
# Serving side (vLLM example, external to Nexus)
vllm serve NousResearch/Hermes-3-Llama-3.1-70B \
  --host 0.0.0.0 --port 8000 --served-model-name hermes-3-70b

# .env (Nexus side)
OLLAMA_BASE_URL=http://vllm-host.internal:8000/v1
OLLAMA_API_KEY=sk-vllm-local     # any non-empty string; ignored by vLLM
OLLAMA_DEFAULT_MODEL=hermes-3-70b

# Request (note: still uses the ollama: prefix)
curl -X POST http://localhost:9000/v1/agent/run \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Find any files over 10MB in this repo.",
    "model_id": "ollama:hermes-3-70b"
  }'
```

The same pattern works for any OpenAI-compatible server: LiteLLM's
proxy, TGI's `--tokenizer-name`, SGLang, or a cloud vendor's
compatibility endpoint. The Covernor + critic + trace pipeline is
unchanged — only the resolved base URL differs.

---

## Registering tools the model can call

The model only ever sees tools that are in the `ToolRegistry` at the
start of the agent loop. Tools are registered in
[`app/core/agent/builtin.py`](../app/core/agent/builtin.py) and seeded
with Covernor policies in `app/main.py::_seed_agent_policies`.

Out of the box, the following tools are available:

| Tool | Covernor action | Seeded policies | Effective default |
|---|---|---|---|
| `shell_exec` | `shell_exec` | `agent-shell-allow` (priority 40, allow, `*`); `*rm*` and `*sudo*` → `require_approval` (priority 3) | Allow, with destructive patterns gated |
| `file_read` | `file_read` | `agent-allow-file-read` (priority 8, allow, `*`) | Allow; paths resolved under `AGENT_WORKSPACE` |
| `file_write` | `file_write` | `agent-allow-file-write` (priority 8, allow, `*`); top-level `approve-file-write` (priority 50, `require_approval`) | Allow — Covernor evaluates in ascending priority, so the agent-scoped policy at priority 8 matches first and wins over the top-level approval gate at priority 50 |
| `web_fetch` | `web_fetch` | `agent-web-fetch-allow` (priority 15, allow, `*`); `*localhost*` and `*127.0.0.1*` → deny (priority 5) | Allow for external URLs, blocked for loopback |
| `search` | `search` | `agent-search-allow` (priority 15, allow, `*`) | Allow |

All of the above live in `_seed_agent_policies` in `app/main.py`.
Covernor orders matches by ascending `priority`, so lower numbers
evaluate first — that's why the `*rm*` / `*sudo*` / `*localhost*`
denies (priority 3 and 5) take precedence over the broad
`agent-shell-allow` / `agent-web-fetch-allow` (priority 15 and 40).
Tighten any of them by POSTing a lower-numbered deny to
`/v1/governance/policies`.

Add your own tool by subclassing `Tool` in
[`app/core/agent/types.py`](../app/core/agent/types.py), registering
it in `ToolRegistry.register()`, and seeding a Covernor policy —
default-deny means the model will not be able to call it until you do.

The registry is serialized into the system prompt so the Hermes model
knows exactly which tool names and argument schemas are available.
Tools that the model calls with an unknown name simply record an
error step and the loop continues.

---

## End-to-end walkthrough

Minimal demo: run Hermes-3 locally via Ollama, ask it a question that
needs a tool call, watch the Covernor + critic + trace surface.

```bash
# 0. Pull Hermes and start Ollama (external to Nexus)
ollama pull hermes3
# Verify: curl http://127.0.0.1:11434/v1/models

# 1. Configure Nexus
cat >> .env <<'EOF'
OLLAMA_BASE_URL=http://127.0.0.1:11434/v1
OLLAMA_DEFAULT_MODEL=hermes3
OLLAMA_LIST_IN_PROVIDERS=true
LOCAL_ONLY=true              # block outbound HTTP; keep everything on-box
MEMORY_ENABLED=true          # exercise the learning loop
EOF
make dev

# 2. Hit the agent endpoint with a task that implies a tool call
curl -X POST http://localhost:9000/v1/agent/agent/run \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $NEXUS_API_KEY" \
  -d '{
    "prompt": "Count how many .py files are in this repo and save the answer to count.txt.",
    "model_id": "ollama:hermes3"
  }'

# Step cap is a global setting, not a per-request flag:
#   AGENT_MAX_STEPS=6  # in .env, then restart

# 3. Inspect the trace stream
nexus status                                # list recent traces
curl http://localhost:9000/v1/traces | jq '.items[0]'

# 4. Verify the hash chain is intact
curl "http://localhost:9000/v1/traces/session/<session_id>/verify-chain" \
  -H "X-API-Key: $NEXUS_API_KEY"

# 5. Check what the agent learned
nexus memory recall --user <user_id>        # any beliefs the extractor picked up
nexus memory stats
```

What you should see in the trace stream:

- Step 1: immune scan → `pass`, score 0.0.
- Step 2: Hermes emits
  `{"action":"tool_call","tool":"shell_exec","arguments":{"command":"find . -name '*.py' | wc -l"}}`.
  Covernor evaluates `shell_exec` against args-as-resource. `agent-shell-allow` matches, `allow`.
- Step 3: tool output recorded. Hermes emits another `tool_call`,
  this time `file_write` for `count.txt`. Covernor allows per
  `agent-allow-file-write`.
- Step 4: Hermes emits `{"action":"final_answer","content":"…"}`.
  Arbiter scores the response; if all critics pass, response is
  released.
- Step 5: Hash-chained trace persisted. If `MEMORY_ENABLED=true`, the
  extractor runs on the user/assistant turn, the skepticism layer
  vets any beliefs, Covernor gates `memory:write:{entity}`, and
  approved beliefs land in the chain.

If any step fails:

- **Immune block** → trace status `blocked`, response suppressed.
- **Covernor deny on a tool call** → that step records `governance: deny`, the loop continues (the model can try another approach).
- **Critic halt** → response blocked, labeling queue gets a new row.
- **Tool error** → step records `success: false`, loop continues.

---

## Model selection guidance

Because Nexus is provider-agnostic, the "best" Hermes-class model is
whichever one meets your latency / cost / quality budget *and*
reliably emits JSON. Rough recommendations, ordered by deployment
size:

| Deployment | Suggested model | Host | Notes |
|---|---|---|---|
| Laptop / demo | `hermes3` (8B Q4) | Ollama | 4-8GB VRAM, ~20 tok/s on an M-series Mac |
| Dev box / single-user | `NousResearch/Hermes-3-Llama-3.1-8B` (fp16) | `LOCAL_HF_MODEL_ID` with CUDA | Single 24GB GPU; deterministic for reproducibility |
| Team / shared dev | Hermes-3-8B on Ollama, or any JSON-reliable 7–13B | Ollama | Shared cache, GPU scheduling via Ollama's queue |
| Production | `NousResearch/Hermes-3-Llama-3.1-70B`, Llama-3.1-70B-Instruct, or a fine-tuned Hermes variant | vLLM or TGI behind a load balancer | Use the `ollama:` route pointed at your vLLM base URL |
| Cost-optimised | DeepSeek-V2.5 (hosted) or Qwen2.5 via any OpenAI-compatible gateway | Provider of your choice | Works identically; Covernor/critic pipeline unchanged |

Whichever you pick, the only correctness requirement is *"can this
model reliably produce JSON matching our two shapes?"* The
`tool_injection_redteam` and `skill_composition` benchmarks in
`tests/eval/` exercise the pipeline with a deterministic mock
provider; run them against your real model by passing
`--provider real` where supported (see `agent_benchmark.py`).

---

## Performance & tuning

A few knobs operators reach for when the Hermes-via-Nexus loop feels
slow or over-strict:

- **Reduce critic load.** If you don't need the full
  `reasoning`+`injection`+`safety`+`quality` tree on every response,
  deactivate or lower-priority the expensive LLM-backed critics via
  `PATCH /v1/critic/registry/{name}`. Heuristic critics run in-process
  and cost nothing.
- **Cap `AGENT_MAX_STEPS`.** The default is conservative. If your
  Hermes model reliably terminates in 3–4 tool calls, lower
  `AGENT_MAX_STEPS` in `.env` to avoid runaway reflection loops. It
  is a global setting today, not a per-request override.
- **Enable the LLM response cache.** Set `LLM_CACHE_ENABLED=true`.
  Identical (prompt, model, system) tuples short-circuit the model
  call entirely — the critic tree still runs. Safe for deterministic
  tasks; skip for anything with a temporal component.
- **Enable memory.** `MEMORY_ENABLED=true` is counter-intuitively a
  latency *win* past the first few runs because skill-recall lets
  the model finish in fewer tool calls on recurring tasks. The
  `agent_benchmark` shows uplift from zero to one-shot final-turn
  answers on the "tool-use reasoning with memory-as-cache" scenario.
- **Use the circuit breaker + fallback chain.** If your primary Hermes
  endpoint flaps, configure the fallback (e.g.
  Gemini → DeepSeek → mock) so the agent stays up while the breaker
  cools down. See `app/core/llm/circuit_breaker.py`.

---

## Troubleshooting

### Model returns prose / Markdown instead of JSON

You're using a base model or a chat-tuned model that wasn't trained
on tool-call JSON. Two fixes, in order of preference:

1. Switch to a Hermes-series or other JSON-trained variant.
2. Prepend a few-shot primer to the user prompt with an example
   turn. Nexus doesn't inject few-shots by default because Hermes
   models don't need them; weaker models often do.

### "Unknown tool: X" in trace results

The model invented a tool name that isn't in the registry. Either
register it (see [Registering tools](#registering-tools-the-model-can-call))
or update the skill / prompt so the model stops suggesting it.
The `run_agent` loop does NOT fail on unknown tools; it records the
error step and keeps going.

### Every step returns `governance: deny`

Default-deny is working and you haven't seeded Covernor policies
for the tool the model is calling. Inspect via
`GET /v1/governance/policies` and add a scoped allow. The
`_seed_agent_policies` function in `app/main.py` shows the canonical
shape.

### Hermes gets stuck in a tool-call loop

The model is emitting tool_call repeatedly without ever producing
a final_answer. Lower `AGENT_MAX_STEPS` in `.env` and tune the
critic tree — a `reasoning` critic with a halt verdict will
short-circuit nonsense loops before they exhaust the step budget.

### Slow first request, fast subsequent requests

Expected for `LOCAL_HF_MODEL_ID`: the first request loads tokenizer
+ weights into VRAM. Trigger a warm-up request at startup if you
need predictable p50 latency.

### Switching OLLAMA_BASE_URL doesn't take effect

The Ollama client is memoized per-process. Restart the Nexus
process (or call `app.core.llm.provider.reset_clients()` in a test
harness) after changing the URL.

### vLLM returns 400 "unknown model"

`OLLAMA_DEFAULT_MODEL` must match vLLM's `--served-model-name`
exactly. vLLM does not auto-resolve model aliases — the request's
`model` field has to be a string vLLM registered.

### I want to see what system prompt Hermes actually received

The final system prompt (including skill/episode/belief recall) is
recorded on every agent trace. Inspect via
`GET /v1/traces/{id}` and look at `pipeline.asflc` or step payloads
in the replay endpoint.

---

## See also

- [docs/openclaw_integration.md](openclaw_integration.md) — sister
  doc; the same governance + audit story, applied to imported
  ClawHub skills rather than tool-calling models.
- [docs/memory.md](memory.md) — the memory layer Hermes models get
  for free (bitemporal beliefs, Beta confidence, causal DAG,
  hash-chain integrity).
- [docs/benchmarks.md](benchmarks.md) — `tool_injection_redteam`,
  `skill_composition`, and `agent_benchmark` receipts.
- [`app/core/llm/provider.py`](../app/core/llm/provider.py) — the
  one file to read if you want to add a new provider shape.
- [`app/agent/agent_loop.py`](../app/agent/agent_loop.py) — the
  ReAct loop that parses Hermes's JSON output and governs each
  step.
