# Nexus Agent — Doctrine Lab upgrade guide (AI agent handoff)

**Purpose:** When opening Nexus to integrate adopted Doctrine Lab safety adapters and export paths (2026-06-30), follow this doc.

**Sister factory:** [doctrine-lab/docs/SISTER_REPO_UPGRADE.md](../thinking-DT/doctrine-lab/docs/SISTER_REPO_UPGRADE.md)  
**Integration smoke report:** [doctrine-lab/data/holdout/integration_smoke_report.md](../thinking-DT/doctrine-lab/data/holdout/integration_smoke_report.md)

## Current integration status (verified 2026-06-30)

| Check | Status |
|-------|--------|
| Injection critic `lora_adapter_path` | `local-lora:injection-mixed-safety-v8-3b` (dev/lab) |
| `LOCAL_LORA_MODELS_ROOT` | Points to `doctrine-lab/data/models` (in `.env`) |
| `critic_scores` on Doctrine export | Flattened via `app/core/critic/scores.py` |
| `tool_injection_redteam` | 18/18 (multi_language BLOCK) |
| `injection_critic_lora_eval` | Registry wires v8 on LLM path |
| GitHub PR CI (`defense-gate` job) | critic export + redteam + lora eval — **active** |
| Cross-repo `integration-smoke` | doctrine-lab weekly CI ([run #28420009831](https://github.com/denial-web/doctrine-lab/actions/runs/28420009831)) |
| Production Ollama path (§6) | **Not deployed** — use for prod ship |

**After registry changes:** restart Nexus so Arbiter cache reloads (~60s TTL).

**Dev vs prod:** Lab uses in-process `local-lora:…` under `LOCAL_LORA_MODELS_ROOT`. Production should use `config.model_id` → `ollama:injection-mixed-safety-v8-3b` (merged weights, no 3B LoRA in the Nexus process).

## Adopted adapters (from factory)

| Model ID | Nexus use |
|----------|-----------|
| `local-lora:injection-mixed-safety-v8-3b` | **Injection critic** (production safety) |
| `local-lora:1c23e153` | Agent regression **gate baseline** — do not replace as live gate without review |

**Retired for OOD safety:** `local-lora:injecagent-safety-3b` — remove from examples/docs if still referenced.

## Upgrade checklist (agent workflow)

### 1. Environment (dev / lab)

Add to `.env` (or export in shell):

```bash
LOCAL_LORA_MODELS_ROOT=/absolute/path/to/doctrine-lab/data/models
DOCTRINE_LAB_URL=http://127.0.0.1:8000
DOCTRINE_LAB_API_KEY=<matches Doctrine Lab API_KEY when set>
NEXUS_API_KEY=<your nexus key>
```

Verify adapter files exist:

```bash
ls "$LOCAL_LORA_MODELS_ROOT/injection-mixed-safety-v8-3b/adapter_model.safetensors"
ls "$LOCAL_LORA_MODELS_ROOT/injection-mixed-safety-v8-3b/decode.json"
```

### 2. Wire injection critic to v8

**Option A — SQLite (dev, already done 2026-06-30):**

```sql
-- nexus.db
UPDATE critic_registry
SET lora_adapter_path = 'local-lora:injection-mixed-safety-v8-3b'
WHERE name = 'injection';
```

**Option B — API (running server):**

```bash
# Get node id
curl -s -H "X-API-Key: $NEXUS_API_KEY" \
  "http://127.0.0.1:9000/api/critic/registry" | jq '.nodes[] | select(.name=="injection")'

curl -X PATCH "http://127.0.0.1:9000/api/critic/registry/<INJECTION_NODE_ID>" \
  -H "X-API-Key: $NEXUS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"lora_adapter_path": "local-lora:injection-mixed-safety-v8-3b"}'
```

**Option C — After factory fine-tune:**

```bash
curl -X POST "http://127.0.0.1:9000/v1/training/promote-adapter" \
  -H "X-API-Key: $NEXUS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"job_id": "<doctrine-job-id>", "node_name": "injection"}'
```

Restart Nexus after any registry change.

### 3. Run Nexus test suite

```bash
cd /path/to/nexus-agent && source venv/bin/activate
export LOCAL_LORA_MODELS_ROOT=/path/to/doctrine-lab/data/models

pytest tests/test_local_lora_critic.py tests/test_critic.py -q
pytest tests/test_critic_scores_export.py -q
python -m tests.eval.tool_injection_redteam --json
pytest tests/eval/injection_critic_lora_eval.py -q
```

Optional live LoRA (M1 GPU, ~3B):

```bash
NEXUS_CRITIC_LIVE=1 pytest tests/eval/injection_critic_lora_eval.py -k live -v
```

### 4. Factory cross-repo gate

```bash
make -C /path/to/thinking-DT/doctrine-lab integration-smoke
```

### 5. Export failures to Doctrine Lab

```bash
curl -X POST "http://127.0.0.1:9000/v1/training/export" \
  -H "X-API-Key: $NEXUS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"send_to_doctrine_lab": true, "origin": "organic"}'
```

Entries must include flat `critic_scores` (handled by `labeler.py` + `doctrine_bridge.py`).

### 6. Production path (before prod ship — not done yet)

Do **not** load 3B LoRA in-process in production if you can avoid it.

**Step A — Merge and serve v8 via Ollama** (on a GPU host or sidecar):

```bash
# 1. Merge adapter + Qwen2.5-3B-Instruct (Doctrine Lab or your merge pipeline)
#    Adapter: doctrine-lab/data/models/injection-mixed-safety-v8-3b/
#    Use decode.json profile from the same directory for generation settings.

# 2. Import merged weights into Ollama (example name)
ollama create injection-mixed-safety-v8-3b -f Modelfile

# 3. Verify
ollama run injection-mixed-safety-v8-3b "Reply OK if loaded."
```

**Step B — Point Nexus injection critic at Ollama** (not `local-lora:`):

```bash
curl -X PATCH "http://127.0.0.1:9000/api/critic/registry/<INJECTION_NODE_ID>" \
  -H "X-API-Key: $NEXUS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "lora_adapter_path": null,
    "config": {"model_id": "ollama:injection-mixed-safety-v8-3b"}
  }'
```

`Arbiter.load_from_registry()` prefers `config.model_id` over `lora_adapter_path` (see `normalize_adapter_model_id` in `app/core/llm/local_lora.py`).

**Step C — Keep immune-layer CI gate**

PR `defense-gate` continues to run `tool_injection_redteam` (deterministic scanner boundary, independent of LoRA/Ollama).

**Step D — Sign-off before deploy**

```bash
source venv/bin/activate
export LOCAL_LORA_MODELS_ROOT=/path/to/doctrine-lab/data/models
pytest tests/test_critic_scores_export.py tests/eval/injection_critic_lora_eval.py -q
python -m tests.eval.tool_injection_redteam --json
make -C /path/to/doctrine-lab cross-project-smoke
```

## Key files (edit map)

| File | What it does |
|------|----------------|
| `app/core/critic/scores.py` | Serialize `critic_scores` for Doctrine import |
| `app/services/doctrine_bridge.py` | HTTP client → `POST /api/datasets/import` |
| `app/core/training/labeler.py` | Labeling queue + `export_for_training` |
| `app/core/critic/arbiter.py` | Loads registry → `LLMInjectionCritic` + `model_id` |
| `app/core/llm/local_lora.py` | Resolves `local-lora:<suffix>` under `LOCAL_LORA_MODELS_ROOT` |
| `app/core/immune/scanner.py` | Tool-call boundary blocking (`is_tool_call_blocked`) |
| `tests/eval/tool_injection_redteam.py` | **100% block** exit gate at MCP boundary |
| `tests/eval/injection_critic_lora_eval.py` | Registry + v8 LLM path smoke |

### CI layout (active)

| Repo | Gate |
|------|------|
| **nexus-agent** (this repo) | PR: `defense-gate` (critic export + `injection_critic_lora_eval` + `tool_injection_redteam`) |
| **doctrine-lab** | PR: `cross-project-smoke` · Weekly: `integration-smoke.yml` |
| **ClawGuard** | PR: `safety:eval` + export contract |

For Nexus defense PRs, **`defense-gate` + `cross-project-smoke`** is sufficient. Full three-repo sweep runs weekly in Doctrine Lab CI.

```bash
# Local factory gate (~2 min)
make -C /path/to/doctrine-lab cross-project-smoke

# Full cross-repo (optional locally; covered by weekly CI)
cd /path/to/doctrine-lab && source venv/bin/activate
make integration-smoke
```

## Do NOT (unless human explicitly requests)

- Promote `injecagent-safety-3b` or real-trace v9–v15 adapters
- Trigger RunPod from Nexus without factory gate pass
- Swap `1c23e153` agent gate baseline silently
- Skip `integration-smoke` before merging defense changes

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `LOCAL_LORA_MODELS_ROOT is not set` | Set env; restart Nexus |
| Critic still uses API model, not LoRA | Check registry `lora_adapter_path`; restart; Arbiter TTL ~60s |
| Doctrine export missing `critic_scores` | Ensure `push_failure` uses `serialize_critic_scores` (agent_loop, pipeline) |
| tool_injection_redteam failure | Fix `app/core/immune/scanner.py`; do not weaken exit gate |
| M1 OOM with critic + other models | One local LoRA at a time; use Ollama for prod |
| Local `integration-smoke` SQLite disk I/O | Stop Nexus; remove `nexus.db`; `alembic upgrade head`; re-seed critics; re-wire v8 |
| Empty `critic_registry` after CI prep | Run `_seed_default_critics` or start Nexus once; wire injection → v8 |
| `make integration-smoke` without venv | `source venv/bin/activate` in doctrine-lab (Python 3.13) |

## Open Nexus in Cursor — agent prompt seed

> Upgrade Nexus per `docs/DOCTRINE_LAB_UPGRADE.md`. Confirm injection critic is `local-lora:injection-mixed-safety-v8-3b` (lab) or `ollama:injection-mixed-safety-v8-3b` (prod), `LOCAL_LORA_MODELS_ROOT` points to doctrine-lab `data/models`, and `critic_scores` export is flat. Run defense-gate tests locally; rely on doctrine-lab weekly `integration-smoke` for cross-repo. Do not retrain or promote non-v8 adapters without factory gate evidence.
