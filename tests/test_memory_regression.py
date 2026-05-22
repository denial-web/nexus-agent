"""
Regression tripwire for Phase 12 memory work.

The whole point of this file is to prove — at CI time — that turning the
memory subsystem OFF (the default) reproduces the pre-memory pipeline
exactly. If anyone accidentally bakes memory behavior into the default
path, these tests fail loudly.

Two-tier contract:

- Tier A (schema + behavior invariance): With MEMORY_ENABLED=False, the
  pipeline must not create memory artifacts, call memory:* Covernor
  policies, or log memory activity. Runs on every pytest invocation.

- Tier B (fixture-frozen pipeline parity): With a deterministic prompt
  and the mock LLM provider, the normalized pipeline result must match a
  golden fixture captured before any memory code landed. Runs on every
  pytest invocation.

Regenerating the golden (only do this deliberately; the whole point is
that it catches drift):

    NEXUS_UPDATE_GOLDEN=1 pytest tests/test_memory_regression.py::TestMemoryRegressionTierB -q

See MEMORY_FLAGSHIP_PLAN.md section 3 ("PHASE 12A — Foundation") for the
design rationale behind these invariants.
"""

import json
import os
from dataclasses import asdict
from difflib import unified_diff
from pathlib import Path
from unittest.mock import patch

import pytest
from app.agent.pipeline import run
from app.config import settings

FIXTURES_DIR = Path(__file__).parent / "fixtures"
GOLDEN_PATH = FIXTURES_DIR / "pipeline_golden.json"

# Deterministic prompt for Tier B. Chosen to:
#   - pass the immune scanner (no injection triggers)
#   - produce a consistent mock LLM response (depends only on len(prompt))
#   - exercise most pipeline stages (critic eval, governance, output scan)
_GOLDEN_PROMPT = (
    "Summarize the core idea of the dependency inversion principle in software engineering in two sentences."
)
_GOLDEN_SESSION = "golden-fixture-session-0001"


# ---------------------------------------------------------------------------
# Normalization — strip or pin every field that varies run-to-run so we can
# byte-compare against a checked-in fixture.
# ---------------------------------------------------------------------------


_DYNAMIC_SENTINEL = "__NORMALIZED__"
_DYNAMIC_NUMERIC_KEYS = {"latency_ms", "duration_ms", "elapsed_ms", "token_count"}
_DYNAMIC_STRING_KEYS = {
    "trace_id",
    "request_id",
    "span_id",
    "created_at",
    "timestamp",
    "prev_hash",
    "trace_hash",
    "response_hash",
    "prompt_hash",
    "policy_id",  # per-test-session seeded policy UUID
}
# Keys whose *contents* are internal telemetry that varies with DB state
# (CriticScore.details tells us which internal code path fired — heuristic
# vs. LLM vs. fallback). That's not a pipeline-level contract, so we flatten
# it to a stable empty dict regardless of what's inside.
_RESET_TO_EMPTY_DICT_KEYS = {"details"}


def _normalize(obj):
    """Recursively scrub dynamic fields from a nested dict/list structure."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key in _DYNAMIC_NUMERIC_KEYS:
                out[key] = 0
            elif key in _DYNAMIC_STRING_KEYS and value is not None:
                out[key] = _DYNAMIC_SENTINEL
            elif key in _RESET_TO_EMPTY_DICT_KEYS:
                out[key] = {}
            else:
                out[key] = _normalize(value)
        return out
    if isinstance(obj, list):
        return [_normalize(x) for x in obj]
    return obj


def _pretty(d) -> str:
    return json.dumps(d, indent=2, sort_keys=True, default=str)


def _diff(actual, expected) -> str:
    a = _pretty(actual).splitlines()
    e = _pretty(expected).splitlines()
    return "\n".join(unified_diff(e, a, fromfile="golden", tofile="actual", lineterm=""))


# ---------------------------------------------------------------------------
# Tier A — schema + behavior invariance
# ---------------------------------------------------------------------------


class TestMemoryRegressionTierA:
    def test_memory_disabled_by_default(self):
        assert settings.MEMORY_ENABLED is False, (
            "MEMORY_ENABLED must default to False. Flipping the default "
            "without explicit opt-in violates the upgrade-not-downgrade "
            "guarantee in MEMORY_FLAGSHIP_PLAN.md."
        )

    def test_no_memory_policy_evaluations(self, db_session):
        """Pipeline must never evaluate memory:* Covernor actions when off."""
        import app.agent.pipeline as pipeline_mod

        seen: list[tuple[str, str | None]] = []
        real_evaluate = pipeline_mod.evaluate_action

        def spy(action_type, resource=None, parameters=None, db_session=None):
            seen.append((action_type, resource))
            return real_evaluate(
                action_type=action_type,
                resource=resource,
                parameters=parameters,
                db_session=db_session,
            )

        with patch.object(pipeline_mod, "evaluate_action", new=spy):
            run("What is 2+2?", db_session=db_session)

        memory_calls = [
            (a, r) for (a, r) in seen if a.startswith("memory") or (r is not None and r.startswith("memory"))
        ]
        assert memory_calls == [], (
            f"Pipeline must not evaluate memory:* actions when MEMORY_ENABLED=False. Saw: {memory_calls}"
        )
        # Sanity check — we did intercept SOMETHING, so the spy is wired up.
        assert len(seen) > 0, "evaluate_action spy saw zero calls — patching target is wrong."

    def test_no_belief_artifacts_on_trace(self, db_session):
        """Trace must not record belief/memory fields when memory is off."""
        from app.models.trace import Trace

        result = run("A reasonable, clean test prompt.", db_session=db_session)
        trace = db_session.query(Trace).filter_by(id=result.trace_id).first()
        assert trace is not None

        # These columns may or may not exist yet depending on migration state.
        # If they exist, they must be null/empty when memory is off.
        for attr in ("beliefs_formed", "beliefs_retrieved", "memory_summary"):
            value = getattr(trace, attr, None)
            assert value in (None, [], {}, ""), f"trace.{attr} must be empty when memory off; got {value!r}"

    def test_no_belief_rows_written(self, db_session):
        """Belief table (once it exists) must stay empty when memory is off."""
        try:
            from app.models.belief import Belief
        except ImportError:
            pytest.skip("Belief model not yet implemented — skip until Phase 12A.W1 lands")

        run("Another clean prompt for testing.", db_session=db_session)
        count = db_session.query(Belief).count()
        assert count == 0, f"beliefs table must stay empty when MEMORY_ENABLED=False; got {count} rows"

    def test_pipeline_result_shape_unchanged(self, db_session):
        """PipelineResult must not grow new required fields when memory off."""
        result = run("Hello shape check.", db_session=db_session)
        d = asdict(result)

        # Canonical field set from pre-memory pipeline. Adding a new field
        # here is fine only if (a) it defaults to None/empty/0 and (b) the
        # Tier B golden is regenerated intentionally.
        required_keys = {
            "trace_id",
            "session_id",
            "status",
            "response",
            "immune_input",
            "immune_output",
            "critic_result",
            "governance",
            "asflc",
            "latency_ms",
            "error",
            "model_id_used",
            "token_count",
            "approval_request_id",
            "run_mode",
            "task_reward_score",
            "user_feedback",
            "total_steps",
            "self_corrections",
            "agent_state",
            "agent_trajectory",
        }
        missing = required_keys - set(d.keys())
        assert not missing, f"PipelineResult lost required fields: {missing}"


# ---------------------------------------------------------------------------
# Tier B — pipeline parity against golden fixture
# ---------------------------------------------------------------------------


class TestMemoryRegressionTierB:
    def test_parity_with_golden(self, db_session):
        """With mock LLM + normalized dynamic fields, output == golden."""
        # Arbiter is cached across tests — invalidate so this test sees a
        # fresh critic tree built from the current DB, not a stale one left
        # over from whatever test ran before us.
        from app.agent.pipeline import invalidate_arbiter_cache

        invalidate_arbiter_cache()

        result = run(
            _GOLDEN_PROMPT,
            session_id=_GOLDEN_SESSION,
            db_session=db_session,
        )
        assert result.status in ("completed", "blocked"), (
            f"Unexpected pipeline status: {result.status} / {result.error}"
        )

        normalized = _normalize(asdict(result))

        if os.environ.get("NEXUS_UPDATE_GOLDEN") == "1":
            FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
            GOLDEN_PATH.write_text(_pretty(normalized) + "\n")
            pytest.skip(f"Golden updated at {GOLDEN_PATH}. Re-run without NEXUS_UPDATE_GOLDEN=1 to verify parity.")

        assert GOLDEN_PATH.exists(), (
            f"Golden fixture missing at {GOLDEN_PATH}. Generate it with:\n"
            f"    NEXUS_UPDATE_GOLDEN=1 pytest "
            f"tests/test_memory_regression.py::TestMemoryRegressionTierB -q"
        )

        expected = json.loads(GOLDEN_PATH.read_text())
        if normalized != expected:
            pytest.fail(
                "Pipeline output diverged from pre-memory golden fixture.\n"
                "If the change is intentional and memory is still OFF by "
                "default, regenerate with NEXUS_UPDATE_GOLDEN=1 and review "
                "the diff carefully.\n\n" + _diff(normalized, expected)
            )
