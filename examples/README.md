# Examples

Runnable demos for Nexus Agent. Start the server first:

```bash
make dev
```

Then run any example:

| Script | What it demonstrates |
|--------|---------------------|
| `basic_run.py` | Send a prompt, inspect the full pipeline audit trail |
| `injection_demo.py` | Test injection detection across 11 languages |
| `governance_demo.py` | Create policies, exercise the approval workflow |
| `multi_model_compare.py` | Compare multiple LLMs with critic scoring |

All examples use only the Python standard library (no extra dependencies). They talk to the running server via HTTP.

If you have `NEXUS_API_KEY` set in your `.env`, export it so the examples can authenticate:

```bash
export NEXUS_API_KEY=your-key-here
```

Without any LLM provider keys configured, the server runs in **mock mode** — all examples work out of the box.
