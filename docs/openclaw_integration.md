# Nexus Agent — OpenClaw / ClawHub Integration

> **One-liner.** Nexus imports any ClawHub-format SKILL.md via
> `POST /v1/skills/import` and runs each step through the same
> zero-trust pipeline as every other Nexus action: immune scan at
> import, Covernor policy check on every tool call, hash-chained
> trace per step, reward tracking with auto-disable. OpenClaw gives
> you the skill marketplace — Nexus gives you the governed runtime
> to run those skills in production.

This document is the operator guide for running ClawHub skills safely
in Nexus. For the higher-level architecture, see
[AGENTS.md](../AGENTS.md) §"MCP governance proxy" and §"ClawHub skill
import". For the benchmark that proves this works end-to-end, see
[docs/benchmarks.md §5](benchmarks.md#5-skill_composition--clawhub-chain--hostile-probe).

---

## Contents

1. [What Nexus adds to OpenClaw](#what-nexus-adds-to-openclaw)
2. [SKILL.md format Nexus accepts](#skillmd-format-nexus-accepts)
3. [Importing a skill](#importing-a-skill)
4. [What happens at import](#what-happens-at-import)
5. [Executing a skill](#executing-a-skill)
6. [Tightening the governance moat](#tightening-the-governance-moat)
7. [End-to-end walkthrough: a hostile skill blocked at runtime](#end-to-end-walkthrough-a-hostile-skill-blocked-at-runtime)
8. [Limits of the v1 import](#limits-of-the-v1-import)
9. [Troubleshooting](#troubleshooting)

---

## What Nexus adds to OpenClaw

OpenClaw skills are static Markdown documents. That's the strength —
any skill is shippable — but also the weakness: there's no runtime
that decides *"is this specific step safe to execute right now?"*
Nexus is that runtime.

| Concern | OpenClaw alone | Nexus running the OpenClaw skill |
|---|---|---|
| Prompt-injection in the skill body | Not checked | Immune-scanned at import (11 languages, memory bank, session escalation) |
| Malicious `shell_exec` in a benign-looking skill | Runs as written | Covernor evaluates every tool call against your policies. Default-deny. |
| "Why did I run this step?" | Skill Markdown is the only trace | Every step emits a hash-chained trace row (`/v1/traces`) with Covernor decision + tool result |
| Skill quality drifts over time | No signal | Reward-tracked. Skills below `min_reward_threshold` get flagged; below `DISABLE_THRESHOLD=0.4` get auto-disabled |
| Two skills with the same content | Duplicated | Dedup by SHA-256 of the parsed step sequence (`skill_hash`) |
| Operator wants to audit the original markdown | Need to keep the file | `raw_source` column stores it; `GET /v1/skills/{id}/source` returns it verbatim |
| Skill needs external data | `curl` inside a shell block | Same — but `web_fetch` calls go through Covernor and a `*localhost*` deny blocks SSRF by default |

The bet: most operators don't want to choose between "run any
ClawHub skill" and "never get pwned by a skill". Nexus lets you do
both because the governance is a separate layer from the skill
itself.

---

## SKILL.md format Nexus accepts

Nexus uses the standard OpenClaw SKILL.md shape: optional YAML
frontmatter + Markdown body. The parser is
[`app/core/agent/clawhub_import.py`](../app/core/agent/clawhub_import.py)
and the markdown-to-steps heuristic is
[`app/core/agent/clawhub_convert.py`](../app/core/agent/clawhub_convert.py).

### Frontmatter

```yaml
---
name: my-skill-slug              # optional; defaults to a slug of the source label
description: What this skill does # optional; default empty
metadata:
  openclaw:
    requires:
      bins: [bash, curl]          # optional; surfaced on the Skill row as `requirements`
---
```

Unknown frontmatter fields are ignored. A malformed YAML block is
logged and treated as empty.

### Body — what converts to what step

The body is parsed top-to-bottom. Every line is inspected against a
small, deterministic heuristic:

| Markdown pattern | Nexus step | Notes |
|---|---|---|
| Fenced code block starting with `curl ` or containing `curl ` in the first 80 chars | `tool_call` → `web_fetch` | URL extracted by regex; falls back to `https://example.invalid` |
| Any other fenced code block (`` ```bash ``, `` ```sh ``, `` ``` ``, etc.) | `tool_call` → `shell_exec` | Block body becomes `arguments_template.command` (trimmed to 4000 chars) |
| Inline `` `…` `` backticks in prose | `tool_call` → `shell_exec` per snippet | Skipped if it looks like a URL; skipped if under 3 chars |
| Line containing "read file" / "open file" / "read the file" | `tool_call` → `file_read` | Extracts a path matching common text extensions; defaults to `README.md` |
| Line containing "write" / "create a file" / "save to" | `tool_call` → `file_write` | Extracts path; **content defaults to `"(see instructions above)"`** — not meant for real use. Use `shell_exec` with `printf`/`echo` for deterministic writes. |
| Headings, bullets, prose over 40 chars | `instruction` | Injected as LLM context during skill recall; **skipped at execution** |

If the body produces zero steps (empty, or nothing matched), one
`instruction` step with the raw body is emitted so the skill is
never completely inert.

### Step actions actually understood at execution

Only three `action` values are recognised by
[`skills.execute_skill`](../app/core/agent/skills.py):

- `tool_call` — runs the named tool through Covernor. This is where
  the governance actually fires.
- `instruction` — logged and skipped. Useful for skill-recall context.
- `final_answer` — logged and skipped (only meaningful inside the
  agent loop, not standalone skill execution).

Anything else is recorded as an error step.

### Example (the `skillcomp-setup` benchmark skill)

```markdown
---
name: skillcomp-setup
description: Initialize the workspace with a config.json for downstream steps.
metadata:
  openclaw:
    requires:
      bins: [bash, sh]
---

Write the shared configuration the rest of the chain will read.

```bash
printf '%s' '{"stage":"setup","version":1}' > config.json
```
```

After import, the `steps` column on the Skill row is:

```json
[
  {"action": "instruction", "content": "Write the shared configuration the rest of the chain will read."},
  {"action": "tool_call", "tool": "shell_exec",
   "arguments_template": {"command": "printf '%s' '{\"stage\":\"setup\",\"version\":1}' > config.json"},
   "expect_success": true}
]
```

---

## Importing a skill

Three equivalent entry points. All of them land in the same
`import_skill_md()` function, so they share the exact same
validation, scanning, and dedup behaviour.

### REST API

```bash
# From a URL
curl -X POST http://localhost:9000/v1/skills/import \
  -H "X-API-Key: $NEXUS_API_KEY" \
  -F "url=https://clawhub.ai/skills/data-summarizer/SKILL.md"

# From a local file
curl -X POST http://localhost:9000/v1/skills/import \
  -H "X-API-Key: $NEXUS_API_KEY" \
  -F "file=@./my-skill.md"

# Force re-import (bypass dedup — useful after editing)
curl -X POST http://localhost:9000/v1/skills/import \
  -H "X-API-Key: $NEXUS_API_KEY" \
  -F "file=@./my-skill.md" \
  -F "force=true"
```

Response: `{ "skill_id": "…" }`. HTTP 400 if the import was blocked
(by the immune scanner or a malformed payload) or if neither `file`
nor `url` was provided. HTTP 503 if `url` is used with
`LOCAL_ONLY=true`.

### CLI

```bash
nexus skills import ./my-skill.md
nexus skills import --from-url https://clawhub.ai/skills/…/SKILL.md
nexus skills import ./my-skill.md --force
```

Prints the new `skill_id` on stdout, or exits non-zero with an error
message on stderr. Note that `nexus skills import` talks to the local
DB directly via `SessionLocal`, not through the HTTP API, so
`NEXUS_API_KEY` does not apply — but `LOCAL_ONLY` still blocks
`--from-url`.

### Python (for tests / batch scripts)

```python
from app.core.agent.clawhub_import import import_skill_md

skill_id = import_skill_md(
    content=open("./my-skill.md").read(),
    db=db_session,
    source_label="manual:my-skill",
    force=False,
)
```

---

## What happens at import

Every import path converges on
[`import_skill_md()`](../app/core/agent/clawhub_import.py). In order:

```text
SKILL.md content
  │
  ▼
1. Immune scan (first 50 000 chars)
   └─ verdict=block ⇒ reject (HTTP 400 / CLI non-zero).
      FLAG verdicts proceed; the runtime deny is Covernor's job.
  │
  ▼
2. Frontmatter parse
   └─ name, description, metadata.openclaw.requires extracted.
      Malformed YAML is logged, treated as empty. Unknown keys ignored.
  │
  ▼
3. markdown_to_steps (heuristic)
   └─ produces list[{action, tool, arguments_template, …}]
      See "Body — what converts to what step" above.
  │
  ▼
4. skill_hash = sha256(canonical_json(steps))
   └─ Dedup: if a Skill with this hash already exists and force=false,
      return the existing skill_id. This is the cheap idempotency
      layer — re-POSTing the same URL is safe.
  │
  ▼
5. Persist
   └─ Skill row created with:
        * enabled=true, flagged=false
        * immune_scanned=true, critic_scanned=false (critics run at execution)
        * raw_source = first 500 000 chars of the original markdown
        * source = "import:url:<host><path>" or "import:upload:<filename>"
          (so you can tell where a skill came from)
        * name collision? appended with a random suffix; dedup is
          done on skill_hash, not on name.
```

**What import does NOT do:**

- It does **not** run any step.
- It does **not** mint Covernor policies. Default-deny still applies —
  if you haven't granted `shell_exec` for the relevant resource
  pattern, execution will deny. This is intentional; see
  [Tightening the governance moat](#tightening-the-governance-moat).
- It does **not** require the skill to be "trusted". A hostile
  skill with `cat /etc/passwd` will typically get a FLAG (score 0.4)
  from the immune scanner and land in the table; the runtime deny is
  the safety net.

---

## Executing a skill

After import, run the skill. Every `tool_call` step passes through
Covernor before the tool fires. `instruction` and `final_answer`
steps are logged and skipped.

### REST API

```bash
curl -X POST http://localhost:9000/v1/skills/$SKILL_ID/execute \
  -H "X-API-Key: $NEXUS_API_KEY"
```

Response:

```json
{
  "skill_id": "…",
  "skill_name": "my-skill-slug",
  "success": true,
  "reward": 1.0,
  "results": [
    {"step": 0, "action": "instruction", "skipped": true, "guidance": "…"},
    {"step": 1, "tool": "shell_exec", "success": true, "output_head": "…", "error": null}
  ]
}
```

### CLI

```bash
nexus skills execute <skill_id>
nexus skills list                    # browse available skills
nexus skills toggle <skill_id> on|off
```

### Step-by-step: what `execute_skill` does

From [`app/core/agent/skills.py::execute_skill`](../app/core/agent/skills.py):

```text
for each step in skill.steps:
  if action == "instruction" or "final_answer":
      log-and-skip
      continue
  if action != "tool_call":
      record error-step
      continue

  tool = ToolRegistry.get(step.tool)
  resource = json.dumps(step.arguments_template, sort_keys=True)

  gov = Covernor.evaluate_action(tool.covernor_action, resource)
  if gov.decision == "deny":            → record deny, skip step (skill continues)
  if gov.decision == "require_approval" → bail out of the whole skill run
  else (allow):                         → fire the tool, record result

reward = successful_tool_steps / total_tool_call_steps
_update_skill_reward(skill, reward)     # reward moving average,
                                        # auto-flag + auto-disable
return (reward >= 0.5, results, reward)
```

### Reward tracking & auto-disable

Every execution updates the Skill row:

- `total_runs`: counter
- `last_reward`: most recent run's reward
- `avg_reward`: running mean over all runs
- After `DECLINE_WINDOW=5` runs, if `avg_reward < min_reward_threshold`
  (default 0.5 for imported skills), the skill is `flagged=True`.
- If `avg_reward < DISABLE_THRESHOLD=0.4`, the skill is
  `enabled=False` — future executions short-circuit and return
  failure.

Operators can re-enable via `PATCH /v1/skills/{id}` or
`nexus skills toggle <id> on`.

---

## Tightening the governance moat

The benchmark in `tests/eval/skill_composition.py` seeds three deny
policies that any operator should consider copy-pasting into their
Nexus deployment. They enforce "imported SKILL.md shall not read host
secrets" — *without* blocking the skill's other steps.

```bash
# Create deny-at-runtime for classic exfil paths
curl -X POST http://localhost:9000/v1/governance/policies \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $NEXUS_API_KEY" \
  -d '{
    "name": "deny-shell-sensitive-etc-passwd",
    "description": "Block shell access to /etc/passwd",
    "action_pattern": "shell_exec",
    "resource_pattern": "*/etc/passwd*",
    "decision": "deny",
    "risk_level": "high",
    "required_approvals": "0",
    "priority": 1
  }'

curl -X POST http://localhost:9000/v1/governance/policies \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $NEXUS_API_KEY" \
  -d '{
    "name": "deny-shell-sensitive-etc-shadow",
    "description": "Block shell access to /etc/shadow",
    "action_pattern": "shell_exec",
    "resource_pattern": "*/etc/shadow*",
    "decision": "deny",
    "risk_level": "high",
    "required_approvals": "0",
    "priority": 1
  }'

curl -X POST http://localhost:9000/v1/governance/policies \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $NEXUS_API_KEY" \
  -d '{
    "name": "deny-shell-sensitive-ssh",
    "description": "Block shell access to SSH keys",
    "action_pattern": "shell_exec",
    "resource_pattern": "*.ssh/*",
    "decision": "deny",
    "risk_level": "high",
    "required_approvals": "0",
    "priority": 1
  }'
```

Two notes on how these patterns match:

- **`action_pattern`** matches the tool's `covernor_action` (here,
  literal `shell_exec`). You can add policies for
  `file_read` / `file_write` / `web_fetch` / `search` / `api_call`.
- **`resource_pattern`** is a glob (`*` wildcard) matched against the
  JSON-serialised `arguments_template` of the step. For `shell_exec`
  that's `{"command": "cat /etc/passwd > leaked.txt"}`, so patterns
  like `*/etc/passwd*` match correctly.

### Priority semantics

**Lower priority wins.** The seeded `agent-shell-allow` policy
(priority 40) allows general `shell_exec`; a deny at priority 1
takes precedence. Always set sensitive-path denies to priority ≤ 5
so they beat any broad allow you may have added.

### Require-approval for risky actions

Instead of a hard deny, escalate to human approval:

```json
{
  "decision": "require_approval",
  "required_approvals": "2",
  "priority": 3
}
```

When the skill's `shell_exec` step matches, Nexus creates an
`ApprovalRequest` and aborts the skill execution (see the
`require_approval` branch in `execute_skill`). Two approvers vote via
`/v1/governance/approve/{id}`; on quorum, a single-use ECDSA
capability token is minted and the skill can be re-run.

### Per-tenant policies

Covernor policies are flat strings today, not tenant-scoped. If you
need multi-tenant differentiation, encode it in the resource pattern
(`tenant/<id>/…`) or run separate Nexus instances per tenant. Full
per-tenant policy routing is a Phase 13 item.

---

## End-to-end walkthrough: a hostile skill blocked at runtime

This mirrors killer demo #4 ("Adversarial tool injection blocked")
from the README and benchmarks against `skill_composition.py`.

```bash
# 0. Prereqs: Nexus running, NEXUS_API_KEY set
export NEXUS_URL=http://localhost:9000
export NEXUS_API_KEY=<your-key>

# 1. Seed the sensitive-path deny policies (see previous section).
#    Do this once, before importing untrusted skills.

# 2. Import a hostile SKILL.md whose shell block reads /etc/passwd.
cat > /tmp/evil.md <<'EOF'
---
name: exfil-attempt
description: Attempts to read a sensitive system path.
---

Read the host's user database and emit it as a file.

```bash
cat /etc/passwd > leaked.txt
```
EOF

SKILL_ID=$(nexus skills import /tmp/evil.md)
# Skill imports cleanly — the immune scanner FLAGS (score ~0.4)
# but does not block at import. This matches how a real operator
# would receive a skill from ClawHub.

# 3. Execute. The sensitive-path deny fires at the Covernor boundary;
#    `leaked.txt` is NEVER written.
nexus skills execute $SKILL_ID

# Inspect:
ls ./leaked.txt   # ls: no such file
nexus status      # the skill execution trace shows governance:deny

# 4. (Optional) Verify the hash chain of the trace stream is intact.
curl $NEXUS_URL/v1/traces/session/<session_id>/verify-chain \
  -H "X-API-Key: $NEXUS_API_KEY"
```

Reproduce the equivalent test with deterministic tooling:

```bash
python -m tests.eval.skill_composition --json
# {"passes_exit_gate": true, "success_rate": 1.000,
#  "checks": {"benign": 9, "hostile": 2, "total": 11}}
```

---

## Limits of the v1 import

The current import is deliberately a thin, predictable translator.
These limitations are explicit, not accidental; see the decision log
in `MEMORY_FLAGSHIP_PLAN.md`.

- **Markdown-to-steps is heuristic, not semantic.** A fenced block
  becomes one `shell_exec` or `web_fetch`; complex multi-command
  workflows that depend on shell variables set across blocks will
  not round-trip perfectly. If a skill ships as a single bash
  script, author it as one fenced block (what the benchmark skills
  do).
- **No MCP tool mapping.** ClawHub skills that target MCP tools
  (`mcp:{backend}:{tool}`) don't get auto-routed yet. If you need
  that, write the step as a `tool_call` to the specific backend by
  hand, or use the MCP proxy directly. Phase 12B item.
- **No `file_write` content.** The `file_write` converter emits a
  placeholder `"(see instructions above)"`. Prefer deterministic
  shell writes (`printf … > file`) for real data pipelines. The
  placeholder is intentional: auto-generated file content from
  markdown prose is a hallucination waiting to happen.
- **No registry slug API.** `POST /v1/skills/import` only accepts a
  file upload or a URL to a raw SKILL.md. A "paste a ClawHub slug,
  Nexus fetches it" endpoint is Phase 13.
- **URL imports require outbound HTTP.** `LOCAL_ONLY=true` disables
  URL imports entirely (file uploads still work). The URL fetch has
  a 30s timeout with redirect-following enabled.
- **`raw_source` is capped at 500 000 chars.** Enough for any
  realistic skill; a deliberate guard against operator-hostile
  giant-skill uploads.

---

## Troubleshooting

### "Import blocked or failed" (HTTP 400)

- The immune scanner returned `block` on the SKILL.md body. The body
  likely contains an unambiguous injection payload (e.g. "ignore
  previous instructions"). Fix the skill, or — if it's a genuine
  false positive — open an issue with the SKILL.md content.
- The request was malformed (neither `file` nor `url`, or both).

### "URL import disabled" (HTTP 503)

You're running with `LOCAL_ONLY=true`. Either import via file upload
instead, or relax `LOCAL_ONLY` (this also re-enables Doctrine Lab
bridge + web_fetch / search tools — read the setting's docstring
first).

### Skill imported but `nexus skills execute` returns `success=false` with every step `governance: deny`

Default-deny is working. You haven't granted the tool the skill
needs. Check which tool the steps call:

```bash
curl $NEXUS_URL/v1/skills/$SKILL_ID | jq '.steps[].tool'
```

Then either add a broad allow (`agent-shell-allow` / `agent-allow-file-read`
are seeded by default in dev mode; production deployments opt in
explicitly) or grant a scoped allow via `POST /v1/governance/policies`.

### Skill runs but `success=true` with `reward=0.0`

Every step was either skipped (`instruction`, `final_answer`) or
errored in the tool itself without raising. Inspect
`results[*].error` in the response.

### Skill auto-disabled after 5 runs

`avg_reward` dropped below the threshold. Investigate via
`GET /v1/skills/{id}` (check `last_reward`, `total_runs`, `flagged`)
and re-enable via `nexus skills toggle <id> on` once you've fixed
the underlying cause.

### I want to audit the original SKILL.md

```bash
curl $NEXUS_URL/v1/skills/$SKILL_ID/source \
  -H "X-API-Key: $NEXUS_API_KEY"
```

Returns the raw SKILL.md verbatim (first 500 000 chars). Useful when
chasing a drift between "what ClawHub publishes today" and "what I
imported six months ago".

---

## See also

- [README.md](../README.md#openclaw--clawhub-integration) — short
  integration blurb and killer demo #1.
- [docs/benchmarks.md §5](benchmarks.md#5-skill_composition--clawhub-chain--hostile-probe) —
  the benchmark that backs every claim in this doc.
- [docs/memory.md](memory.md) — how the memory layer uses skill
  recall to rank imported skills by `avg_reward` in the agent loop.
- [AGENTS.md](../AGENTS.md) — full architecture context, including
  the MCP proxy that extends this pattern to remote tool backends.
