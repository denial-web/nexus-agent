# Doctrine Lab closed loop

Nexus exports labeled failure traces to Doctrine Lab; trained adapters return via the critic registry.

See the full runbook: [doctrine-lab/docs/CLOSED_LOOP_RUNBOOK.md](../../thinking-DT/doctrine-lab/docs/CLOSED_LOOP_RUNBOOK.md)

## Quick reference

1. `POST /v1/training/export` with `send_to_doctrine_lab: true`
2. Doctrine Lab trains + gates (`make injection-gate`, `make eval-regression`)
3. `POST /v1/training/promote-adapter` or patch `/api/critic/registry/{id}` with `lora_adapter_path`
4. Set `LOCAL_LORA_MODELS_ROOT` to Doctrine Lab `data/models/` (lab). **Adopted injection critic:** `local-lora:injection-mixed-safety-v8-3b` (v8 champion; OOD FNR 0, InjecAgent 0/48 rescored). Use `ollama:` (prod) for out-of-process serving.
