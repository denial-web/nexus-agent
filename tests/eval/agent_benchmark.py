"""
Agent benchmark — Nexus-with-memory vs Nexus-without, on a curated
task set that directly exercises Phase 12 belief memory.

This is the closest thing we have to a "does the memory subsystem
actually make the agent better?" measurement. Every other benchmark
in `tests/eval/` proves memory INTERNALS work (bitemporal reads,
skepticism, derivation DAG, tool-call governance); this one proves
the end-to-end agent loop benefits from having them wired together.

Three sub-scenarios, chosen to isolate different memory-layer claims:

1. **Multi-turn recall** (`multiturn_memory`)
       Turn 1: user states a personal fact ("my favorite color is blue").
       Turn 2: user asks back ("what is my favorite color?").
   Memory-on should surface the belief via `_retrieve_beliefs` in the
   system prompt so the agent answers "blue". Memory-off has no
   retrieval hook and falls back to a non-answer.

2. **Tool-use + memory-as-cache** (`tool_use_reasoning`)
       Turn 1: user asks a question that requires a `file_read`.
               Agent reads the file, forms a belief about its contents,
               answers correctly.
       Turn 2: the file is DELETED (simulating an ephemeral resource),
               and the user asks a follow-up that needs the same fact.
   Memory-on recalls the belief and answers without a re-read.
   Memory-off must re-call the tool, hits a "file not found" and
   fails. This is the GAIA-lite-style signal — not "does the agent
   use tools" (both do) but "does memory let it avoid redundant /
   impossible tool calls".

3. **Preference learning** (`preference_learning`)
       Turn 1: user expresses a style preference ("keep answers short").
       Turn 2: user asks an unrelated general question.
   Memory-on sees the preference belief in retrieval and the mock
   agent mirrors that style (short response). Memory-off replies
   with the default verbose style.

Each scenario runs twice against the real `app.agent.agent_loop.run_agent`:
once with `MEMORY_ENABLED=False`, once with `MEMORY_ENABLED=True`. The
difference in per-task score is the uplift.

EXIT GATE (MEMORY_FLAGSHIP_PLAN.md §4):  uplift ≥ 0.10 (10 percentage
points, absolute). Headline metric: `uplift = with_memory - without_memory`,
range [-1, 1]. A negative uplift would mean memory made the agent worse
and is an immediate FAIL.

**Providers**

    --provider mock   (default, CI-safe, fully deterministic, no keys)
    --provider real   (opt-in; requires a configured LLM key; used in
                       nightly workflows for a spot-check headline number)

The mock provider is a keyword-driven router built specifically for
this benchmark. It reads the full planner prompt (system + recent
messages) that `run_agent` sends to `generate()`, extracts any
retrieved-belief block, decides whether to emit a `tool_call` or a
`final_answer`, and phrases the answer to either cite the belief or
admit ignorance. That is enough to exercise the memory wiring
end-to-end without introducing LLM variance into the CI signal.

Real-provider mode runs the exact same scenarios and scoring against
a configured Gemini / OpenAI / DeepSeek provider. The exit gate is
identical, but real-mode is flagged `requires_key` in the JSON report
so the nightly workflow can skip it gracefully when the CI secret is
absent.

CLI:

    python -m tests.eval.agent_benchmark --json                    # mock
    python -m tests.eval.agent_benchmark --provider real --json    # real
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pytest
from app.config import settings
from app.core.llm.models import LLMResponse

# Import app.main at module top so `configure_logging()` fires BEFORE any
# call to `reroute_logging_to_stderr()` in `_main()`. Otherwise the first
# late import of app.main inside `_seed_required_policies` re-attaches a
# stdout handler *after* reroute cleaned up, and `--json` consumers get
# log lines mixed into the JSON body.
from app.main import _seed_agent_policies, _seed_memory_policies  # noqa: E402

# ---------------------------------------------------------------------------
# Mock provider — keyword-driven, deterministic, belief-aware
# ---------------------------------------------------------------------------
#
# `app.agent.agent_loop.run_agent` constructs a planner prompt that includes:
#
#     system prompt (with belief section when present)
#     + "Conversation (recent):\n<last 4 messages as JSON>"
#     + "What is the next JSON action?"
#
# The mock below parses THAT prompt literally — it is intentionally
# coupled to agent_loop's prompt shape so a drift there surfaces as a
# benchmark failure rather than a silent regression. If agent_loop
# rewords the "Known beliefs about the user" header or the
# "Conversation (recent):" separator, update _parse_planner_prompt
# and re-record the scenario expectations.


_BELIEF_HEADER = "Known beliefs about the user (high-confidence, current):"
_CONVERSATION_HEADER = "Conversation (recent):"

_BELIEF_ROW_RE = re.compile(
    r"^-\s*\[[^\]]+,\s*conf=[\d.]+\]\s+(?P<entity>\S+)\s+(?P<predicate>\S+)\s*=\s*(?P<value>.+?)\s*$"
)

# Match the two shapes `run_agent` uses for tool-result messages (role=user):
#   "Tool <name> result:\n..."          (normal loop — agent_loop.py line ~912)
#   "Tool result (<name>):\n..."        (resume-from-approval path, line ~593)
# Must cover both so the parser never mistakes a tool result for the
# user's own utterance.
_TOOL_RESULT_PREFIX_RE = re.compile(r"^Tool (?:\S+ result:|result \()", re.MULTILINE)


@dataclass
class PlannerView:
    """What the mock LLM needs from `run_agent`'s planner prompt.

    Only the fields the mock's routing rules actually use are
    extracted. Keeping this struct small makes it cheap to add new
    rules and makes the test that verifies prompt-coupling readable.
    """

    beliefs: dict[tuple[str, str], str]  # (entity, predicate) -> value
    last_user_message: str
    last_tool_result_text: str | None


def _parse_planner_prompt(prompt: str) -> PlannerView:
    """Extract beliefs + most recent user message from the planner prompt.

    Tolerant parser — missing sections yield empty defaults so the
    mock continues to produce a reasonable action even if agent_loop's
    formatting drifts slightly (which is also an explicit benchmark
    scenario at the integration test level)."""
    beliefs: dict[tuple[str, str], str] = {}
    last_user = ""
    last_tool_result: str | None = None

    if _BELIEF_HEADER in prompt:
        tail = prompt.split(_BELIEF_HEADER, 1)[1]
        # Belief block ends at the first blank line OR the conversation header.
        body = tail.split(_CONVERSATION_HEADER, 1)[0]
        for line in body.splitlines():
            m = _BELIEF_ROW_RE.match(line.strip())
            if m:
                beliefs[(m.group("entity"), m.group("predicate"))] = m.group("value").strip()

    if _CONVERSATION_HEADER in prompt:
        convo = prompt.split(_CONVERSATION_HEADER, 1)[1]
        # The convo block is `<json>\n\nWhat is the next JSON action?` — split off
        # the trailing instruction.
        convo = convo.rsplit("What is the next JSON action?", 1)[0].strip()
        try:
            msgs = json.loads(convo)
            if isinstance(msgs, list):
                # Walk BACKWARDS: most recent user message wins. Tool
                # results are posted as role="user" with the "Tool
                # result" prefix — skip those and remember them
                # separately.
                for m in reversed(msgs):
                    if not isinstance(m, dict):
                        continue
                    role = m.get("role")
                    content = str(m.get("content", ""))
                    if role != "user":
                        continue
                    if _TOOL_RESULT_PREFIX_RE.match(content):
                        if last_tool_result is None:
                            last_tool_result = content
                        continue
                    if not last_user:
                        last_user = content
        except json.JSONDecodeError:
            pass

    return PlannerView(
        beliefs=beliefs,
        last_user_message=last_user,
        last_tool_result_text=last_tool_result,
    )


def _bench_mock_action(prompt: str) -> dict[str, Any]:
    """Route a planner-view to an agent JSON action.

    The routing rules mirror the three scenarios exactly; keep them
    in sync with `SCENARIOS` below when adding new tasks.
    """
    view = _parse_planner_prompt(prompt)
    q = view.last_user_message.lower()

    # --- scenario 1 & 3: belief-seeded answers --------------------------
    # A retrieved belief immediately changes the answer shape. We match
    # on predicate rather than phrasing so spelling variations in the
    # question don't break the rule.

    def _belief(predicate_suffix: str) -> str | None:
        for (_entity, pred), value in view.beliefs.items():
            if pred.endswith(predicate_suffix) or predicate_suffix in pred:
                return value
        return None

    # Scenario 1 — "what is my favorite color?" style questions.
    if "favorite color" in q or ("color" in q and ("my" in q or "favorite" in q)):
        color = _belief("color")
        if color:
            return {
                "action": "final_answer",
                "content": f"Your favorite color is {color}.",
            }
        return {
            "action": "final_answer",
            "content": "I don't have that information.",
        }

    # Scenario 2 — "what's the version?" / "version of config".
    # If a retrieved belief already knows the version, answer directly.
    # Otherwise fall through to a tool-call plan.
    if "version" in q and ("config" in q or "what" in q):
        ver = _belief("version")
        if ver:
            return {
                "action": "final_answer",
                "content": f"The version is {ver}.",
            }
        # If we already got a tool result this turn (we read the file
        # on step 0), extract the version from the tool output and
        # answer. Otherwise issue the tool call.
        if view.last_tool_result_text:
            m = re.search(r'"version"\s*:\s*"([^"]+)"', view.last_tool_result_text)
            if m:
                return {
                    "action": "final_answer",
                    "content": f"The version is {m.group(1)}.",
                }
            # Tool result returned but no parseable version — admit defeat
            # rather than loop forever.
            return {
                "action": "final_answer",
                "content": "I couldn't determine the version.",
            }
        return {
            "action": "tool_call",
            "tool": "file_read",
            "arguments": {"path": "agentbench_config.json"},
        }

    # Scenario 3 — "tell me about X". Look for a style preference.
    if (
        any(kw in q for kw in ("tell me about", "explain", "describe", "what is "))
        and "color" not in q
        and "version" not in q
    ):
        style = _belief("answer_style") or _belief("style")
        topic = _extract_topic(q)
        if style and "short" in style.lower():
            return {
                "action": "final_answer",
                "content": f"{topic.capitalize()} is a broad field; the key point is it matters.",
            }
        if style and ("long" in style.lower() or "verbose" in style.lower()):
            return {
                "action": "final_answer",
                "content": _verbose_answer(topic),
            }
        # Default: verbose. This is the no-memory baseline.
        return {
            "action": "final_answer",
            "content": _verbose_answer(topic),
        }

    # --- setup turns --------------------------------------------------
    # User statements that the agent simply acknowledges — the belief
    # extractor hooks in post-turn via `_extract_and_persist_beliefs`,
    # so the mock only needs a plausible assistant reply here.
    if any(kw in q for kw in ("my favorite", "remember", "i prefer", "keep answers")):
        return {
            "action": "final_answer",
            "content": "Acknowledged. I'll keep that in mind.",
        }

    return {
        "action": "final_answer",
        "content": "Understood.",
    }


_TOPIC_STRIP = re.compile(
    r"^(tell me about|explain|describe|what is)\s+",
    re.IGNORECASE,
)


def _extract_topic(q: str) -> str:
    stripped = _TOPIC_STRIP.sub("", q).strip().rstrip("?.!")
    return stripped or "the topic"


def _verbose_answer(topic: str) -> str:
    """The no-memory baseline reply for scenario 3. Deliberately ~400 chars
    so a `len < 200` style gate can distinguish it from the `short` reply."""
    return (
        f"{topic.capitalize()} is a rich area with a long history. "
        "There are many schools of thought, a large body of literature, "
        "and active research communities exploring edge cases. Practitioners "
        "typically consider multiple tradeoffs when applying it in practice, "
        "including correctness, performance, and cost. For a thorough "
        "treatment, consult primary sources across several disciplines."
    )


def _bench_generate_factory(*, log: list[dict[str, Any]]):
    """Build a drop-in replacement for `generate()` that the benchmark
    installs via monkeypatching. Logs every call so scoring can
    distinguish "responded via memory" from "responded via tool call"."""

    def _bench_generate(
        prompt: str,
        *,
        model_id: str | None = None,
        system_prompt: str | None = None,
        **_: Any,
    ) -> LLMResponse:
        # Extractor calls use a DIFFERENT system prompt shape — detect them.
        # The extractor's system prompt starts with "You extract durable
        # beliefs". We satisfy those separately: our mock extractor
        # returns pre-scripted drafts keyed on visible facts in the
        # user_message, so the benchmark doesn't need the real
        # extractor's LLM call to fire.
        is_extractor_call = bool(system_prompt and system_prompt.startswith("You extract durable"))
        is_reflection_call = "Tool `" in prompt and "finished." in prompt

        if is_extractor_call:
            text = _mock_extractor_response(prompt)
            log.append({"kind": "extract", "text_head": text[:80]})
            return LLMResponse(
                text=text,
                model_id="bench-mock-extractor",
                token_count=0,
                latency_ms=0.0,
                provider="mock",
                raw_response=None,
                request_id=None,
            )

        if is_reflection_call:
            log.append({"kind": "reflect"})
            return LLMResponse(
                text="Proceeding.",
                model_id="bench-mock-reflector",
                token_count=0,
                latency_ms=0.0,
                provider="mock",
                raw_response=None,
                request_id=None,
            )

        action = _bench_mock_action(prompt)
        log.append({"kind": "plan", "action": action.get("action")})
        return LLMResponse(
            text=json.dumps(action),
            model_id="bench-mock-planner",
            token_count=0,
            latency_ms=0.0,
            provider="mock",
            raw_response=None,
            request_id=None,
        )

    return _bench_generate


_EXTRACT_USER_RE = re.compile(r"User message:\s*(?P<body>.+?)\s*Assistant response:", re.DOTALL)


def _mock_extractor_response(prompt: str) -> str:
    """Script the extractor's reply based on visible fact phrases in the
    user message.

    Matches a tight set of patterns so it stays deterministic:
      * "my favorite color is <X>"  -> preference(alice, favorite_color)
      * "keep answers short"        -> preference(alice, answer_style=short)
      * '"version": "<X>"'          -> state(agentbench_config, version)

    Anything else returns `[]` (the extractor contract for "nothing
    durable"). This is deliberately narrower than the real extractor —
    the benchmark only asserts the retrieval pipeline works, not that
    the extractor reads minds.
    """
    m = _EXTRACT_USER_RE.search(prompt)
    user_msg = m.group("body") if m else ""

    drafts: list[dict[str, Any]] = []

    color_m = re.search(r"my\s+favorite\s+color\s+is\s+([a-zA-Z]+)", user_msg, re.IGNORECASE)
    if color_m:
        drafts.append(
            {
                "entity": "alice",
                "predicate": "favorite_color",
                "value": color_m.group(1).lower(),
                "entity_type": "preference",
                "confidence": 0.9,
                "rationale": "user stated directly",
            }
        )

    if re.search(r"keep\s+answers?\s+short", user_msg, re.IGNORECASE) or re.search(
        r"(i\s+prefer|i\s+like)\s+(short|concise|brief)\s+answers?",
        user_msg,
        re.IGNORECASE,
    ):
        drafts.append(
            {
                "entity": "alice",
                "predicate": "answer_style",
                "value": "short",
                "entity_type": "preference",
                "confidence": 0.9,
                "rationale": "user stated style preference",
            }
        )

    # Scenario 2: the assistant's response (or the tool output if
    # included) mentions a version. Extract as `state` so the
    # retrieval layer keys it under config context, not preferences.
    # We match the assistant's natural-language reply ("The version
    # is 3.14.") OR a raw JSON `"version":"…"` — whichever surfaces
    # first wins.
    ver_m = re.search(
        r'(?:version\s+is\s+|"version"\s*:\s*")([0-9A-Za-z._+-]+)',
        prompt,
        re.IGNORECASE,
    )
    if ver_m:
        drafts.append(
            {
                "entity": "agentbench_config",
                "predicate": "version",
                "value": ver_m.group(1),
                "entity_type": "state",
                "confidence": 0.95,
                "rationale": "observed from assistant response",
            }
        )

    return json.dumps(drafts)


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------


@dataclass
class Turn:
    prompt: str
    # A callable that, given the run result and workspace path, returns
    # a (scored, reason) tuple. `scored` is the per-turn score in [0, 1].
    score: Any  # callable; kept as Any to avoid importing types eagerly
    # Side-effect to run just before this turn — used to delete the
    # config file before scenario-2 turn 2, for example.
    pre_turn: Any | None = None


@dataclass
class Scenario:
    slug: str
    user_id_suffix: str
    turns: list[Turn]


def _score_color(result, workspace: Path) -> tuple[float, str]:
    """Scenario 1 turn 2: response must mention the color 'blue'."""
    content = (result.response or "").lower()
    if "blue" in content:
        return 1.0, "contains 'blue'"
    return 0.0, f"missing 'blue' in: {content[:80]!r}"


def _score_acknowledge(_result, _workspace: Path) -> tuple[float, str]:
    """Setup turns always score 1.0 — their job is to plant a belief,
    not to produce a correct answer. The real test is the follow-up."""
    return 1.0, "setup turn"


def _score_version(result, workspace: Path) -> tuple[float, str]:
    """Scenario 2 turn 2: must answer with the correct version (3.14)
    WITHOUT having re-read the file (which is now deleted)."""
    content = (result.response or "").lower()
    if "3.14" in content:
        return 1.0, "answered 3.14 without a fresh read"
    return 0.0, f"did not recover version: {content[:80]!r}"


def _score_short_answer(result, _workspace: Path) -> tuple[float, str]:
    """Scenario 3 turn 2: answer length under 200 chars = short."""
    content = result.response or ""
    if 0 < len(content) < 200:
        return 1.0, f"short ({len(content)} chars)"
    return 0.0, f"not short ({len(content)} chars)"


def _delete_config(workspace: Path) -> None:
    """Pre-turn action for scenario 2 turn 2: yank the config file.
    A memory-off run MUST now fail; a memory-on run MUST still succeed
    because it cached the version as a belief."""
    cfg = workspace / "agentbench_config.json"
    if cfg.exists():
        cfg.unlink()


def _write_config(workspace: Path) -> None:
    """Pre-turn hook for scenario 2 turn 1 — make the fixture available."""
    (workspace / "agentbench_config.json").write_text(
        '{"version": "3.14", "name": "agentbench"}',
        encoding="utf-8",
    )


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        slug="multiturn_memory",
        user_id_suffix="color",
        turns=[
            Turn(
                prompt="Please remember: my favorite color is blue.",
                score=_score_acknowledge,
            ),
            Turn(
                prompt="What is my favorite color?",
                score=_score_color,
            ),
        ],
    ),
    Scenario(
        slug="tool_use_reasoning",
        user_id_suffix="version",
        turns=[
            Turn(
                prompt="Read agentbench_config.json and tell me the version.",
                score=_score_acknowledge,
                pre_turn=_write_config,
            ),
            Turn(
                prompt="What version was the config?",
                score=_score_version,
                pre_turn=_delete_config,
            ),
        ],
    ),
    Scenario(
        slug="preference_learning",
        user_id_suffix="style",
        turns=[
            Turn(
                prompt="Please keep answers short from now on.",
                score=_score_acknowledge,
            ),
            Turn(
                prompt="Tell me about quantum computing.",
                score=_score_short_answer,
            ),
        ],
    ),
)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TurnOutcome:
    turn_index: int
    prompt: str
    response_head: str
    score: float
    reason: str


@dataclass
class ScenarioRun:
    slug: str
    memory_enabled: bool
    turn_outcomes: list[TurnOutcome] = field(default_factory=list)
    aggregate_score: float = 0.0


@dataclass
class ScenarioComparison:
    slug: str
    without_memory: ScenarioRun
    with_memory: ScenarioRun

    @property
    def uplift(self) -> float:
        """Per-scenario uplift: score difference on the FINAL (test) turn.
        Setup turns always pass, so only the last turn matters for the
        memory signal."""
        last_off = self.without_memory.turn_outcomes[-1].score if self.without_memory.turn_outcomes else 0.0
        last_on = self.with_memory.turn_outcomes[-1].score if self.with_memory.turn_outcomes else 0.0
        return last_on - last_off


@dataclass
class AgentBenchmarkResult:
    provider: str
    scenarios: list[ScenarioComparison] = field(default_factory=list)
    requires_key: bool = False

    @property
    def average_without_memory(self) -> float:
        """Average per-scenario final-turn score under MEMORY_ENABLED=False."""
        if not self.scenarios:
            return 0.0
        last_scores = [
            s.without_memory.turn_outcomes[-1].score for s in self.scenarios if s.without_memory.turn_outcomes
        ]
        return sum(last_scores) / len(last_scores) if last_scores else 0.0

    @property
    def average_with_memory(self) -> float:
        if not self.scenarios:
            return 0.0
        last_scores = [s.with_memory.turn_outcomes[-1].score for s in self.scenarios if s.with_memory.turn_outcomes]
        return sum(last_scores) / len(last_scores) if last_scores else 0.0

    @property
    def uplift(self) -> float:
        return self.average_with_memory - self.average_without_memory

    @property
    def passes_exit_gate(self) -> bool:
        # Require both a ≥10pp uplift AND strictly positive with-memory
        # average. Otherwise a scenario where both modes fail at 0 would
        # formally "pass" (uplift=0 is not >= 0.10, so the gate catches
        # that), but be paranoid.
        return self.uplift >= 0.10 and self.average_with_memory > 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "benchmark": "agent_benchmark",
            "provider": self.provider,
            "requires_key": self.requires_key,
            "average_without_memory": round(self.average_without_memory, 4),
            "average_with_memory": round(self.average_with_memory, 4),
            "uplift": round(self.uplift, 4),
            "passes_exit_gate": self.passes_exit_gate,
            "scenarios": [
                {
                    "slug": s.slug,
                    "uplift": round(s.uplift, 4),
                    "without_memory": asdict(s.without_memory),
                    "with_memory": asdict(s.with_memory),
                }
                for s in self.scenarios
            ],
        }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _install_mock_llm(monkeypatch, call_log: list[dict[str, Any]]) -> None:
    """Install the benchmark mock over every module that bound
    `generate` at import time.

    Three patch sites matter:
      1. `app.agent.agent_loop.generate` — the planner + reflection calls.
      2. `app.agent.agent_loop._resolve_route` — forces the non-mock
         branch so `generate()` is actually invoked (otherwise the loop
         bypasses generate() entirely when all API keys are blank).
      3. `app.core.memory.extractor.generate` — the belief extractor.

    Miss any one of these and the benchmark silently degrades — the
    retrieval path yields empty beliefs (because the extractor didn't
    fire) or the planner gets the stock mock JSON (because the bypass
    fired). Keep all three in sync if agent_loop's internal imports
    change.
    """
    from app.agent import agent_loop
    from app.core.memory import extractor as mem_extractor

    fake_generate = _bench_generate_factory(log=call_log)

    monkeypatch.setattr(agent_loop, "generate", fake_generate)
    monkeypatch.setattr(
        agent_loop,
        "_resolve_route",
        lambda model_id: ("gemini", "gemini-bench-mock", "fake-key-for-bench"),
    )
    monkeypatch.setattr(mem_extractor, "generate", fake_generate)


def _seed_required_policies(db) -> None:
    """Covernor policies the agent loop + extractor need.

    - `respond/chat` — final-answer path (seeded globally by conftest
      via `allow-chat-respond`, but we re-seed defensively in the CLI
      path which uses its own in-memory DB).
    - `file_read` — scenario 2 turn 1 needs to read the config.
    - `memory:write:preference` — already seeded by `_seed_memory_policies`.
    - `memory:write:state` — scenario 2's version belief is entity_type=state.
    """
    from app.models.policy import Policy

    _seed_agent_policies(db)
    _seed_memory_policies(db)
    if db.query(Policy).filter_by(name="allow-chat-respond").first() is None:
        db.add(
            Policy(
                name="allow-chat-respond",
                description="Allow chat final-answer",
                action_pattern="respond",
                resource_pattern="chat",
                decision="allow",
                risk_level="low",
                required_approvals="0",
                priority=10,
            )
        )
    if db.query(Policy).filter_by(name="bench-allow-state-memory").first() is None:
        db.add(
            Policy(
                name="bench-allow-state-memory",
                description="agent_benchmark — allow state-type belief writes (config versions)",
                action_pattern="memory:write:state",
                resource_pattern="*",
                decision="allow",
                risk_level="low",
                required_approvals="0",
                priority=50,
                is_active=True,
            )
        )
    db.commit()


def _run_one_scenario(
    scenario: Scenario,
    *,
    db,
    workspace: Path,
    memory_enabled: bool,
    monkeypatch,
) -> ScenarioRun:
    """Play a scenario through to its last turn under a single memory mode."""
    from app.agent.agent_loop import run_agent

    # Each (scenario, memory_mode) gets a unique user_id so retrieval
    # in memory-on mode only sees beliefs the SAME run planted.
    user_id = f"bench-{scenario.slug}-{'on' if memory_enabled else 'off'}-{uuid.uuid4().hex[:6]}"

    # Flip the flag via monkeypatch so teardown restores it and the
    # regression tripwire doesn't see spill-over.
    monkeypatch.setattr(settings, "MEMORY_ENABLED", memory_enabled)

    # Point the agent's workspace at our tmp dir. It's a settings field,
    # so we use monkeypatch.setattr to auto-restore on teardown.
    monkeypatch.setattr(settings, "AGENT_WORKSPACE", str(workspace))

    # Keep planner recursion short — the mock is zero-latency but we
    # don't want a runaway if the mock ever returns a tool_call loop.
    monkeypatch.setattr(settings, "AGENT_MAX_STEPS", 4)

    run = ScenarioRun(slug=scenario.slug, memory_enabled=memory_enabled)
    session_id = uuid.uuid4().hex

    for i, turn in enumerate(scenario.turns):
        if turn.pre_turn is not None:
            turn.pre_turn(workspace)
        result = run_agent(
            prompt=turn.prompt,
            session_id=session_id,
            db_session=db,
            user_id=user_id,
        )
        score, reason = turn.score(result, workspace)
        run.turn_outcomes.append(
            TurnOutcome(
                turn_index=i,
                prompt=turn.prompt,
                response_head=(result.response or "")[:200],
                score=score,
                reason=reason,
            )
        )
    run.aggregate_score = sum(t.score for t in run.turn_outcomes) / len(run.turn_outcomes) if run.turn_outcomes else 0.0
    return run


def run_agent_benchmark(
    *,
    db,
    workspace: Path,
    monkeypatch,
    provider: str = "mock",
) -> AgentBenchmarkResult:
    """Run every scenario twice (off/on) and assemble the comparison."""
    call_log: list[dict[str, Any]] = []
    if provider == "mock":
        _install_mock_llm(monkeypatch, call_log)
    # provider == "real" — no patching; caller is expected to have
    # configured a live API key upstream. The benchmark surface is
    # otherwise identical.

    _seed_required_policies(db)

    result = AgentBenchmarkResult(provider=provider, requires_key=(provider != "mock"))
    for scenario in SCENARIOS:
        off = _run_one_scenario(
            scenario,
            db=db,
            workspace=workspace,
            memory_enabled=False,
            monkeypatch=monkeypatch,
        )
        on = _run_one_scenario(
            scenario,
            db=db,
            workspace=workspace,
            memory_enabled=True,
            monkeypatch=monkeypatch,
        )
        result.scenarios.append(ScenarioComparison(slug=scenario.slug, without_memory=off, with_memory=on))
    return result


# ---------------------------------------------------------------------------
# Pytest entrypoints
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "agent-bench"
    ws.mkdir()
    return ws


def test_agent_benchmark_exit_gate(db_session, agent_workspace, monkeypatch):
    """Phase 12B exit gate: uplift ≥ 10 percentage points between
    memory-on and memory-off on the three curated scenarios."""
    result = run_agent_benchmark(
        db=db_session,
        workspace=agent_workspace,
        monkeypatch=monkeypatch,
        provider="mock",
    )
    assert result.scenarios, "scenarios did not run"
    assert result.passes_exit_gate, (
        f"uplift={result.uplift:.3f} (with={result.average_with_memory:.3f}, "
        f"without={result.average_without_memory:.3f}) below 0.10 gate. "
        f"scenarios={[(s.slug, s.uplift) for s in result.scenarios]}"
    )


def test_agent_benchmark_mock_is_belief_aware(db_session, agent_workspace, monkeypatch):
    """Sanity-check the mock parses retrieval-formatted belief rows.
    If this drifts, `test_agent_benchmark_exit_gate` collapses to
    'memory-off answered correctly' (= no uplift) rather than the
    intended signal."""
    # Synthesize a planner prompt like `run_agent` would emit.
    prompt = (
        "You are a governed agent...\n\n"
        "Known beliefs about the user (high-confidence, current):\n"
        "- [preference, conf=0.90] alice favorite_color = blue\n"
        "- [preference, conf=0.90] alice answer_style = short\n\n"
        "Conversation (recent):\n"
        '[{"role": "user", "content": "what is my favorite color?"}]\n\n'
        "What is the next JSON action?"
    )
    view = _parse_planner_prompt(prompt)
    assert view.beliefs[("alice", "favorite_color")] == "blue"
    assert view.beliefs[("alice", "answer_style")] == "short"
    assert view.last_user_message == "what is my favorite color?"
    action = _bench_mock_action(prompt)
    assert action["action"] == "final_answer"
    assert "blue" in action["content"].lower()


def test_agent_benchmark_schema_stable():
    result = AgentBenchmarkResult(provider="mock")
    body = result.to_json()
    assert set(body.keys()) == {
        "benchmark",
        "provider",
        "requires_key",
        "average_without_memory",
        "average_with_memory",
        "uplift",
        "passes_exit_gate",
        "scenarios",
    }
    assert body["benchmark"] == "agent_benchmark"
    # Empty result must not claim to pass.
    assert body["passes_exit_gate"] is False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    from tests.eval import reroute_logging_to_stderr

    reroute_logging_to_stderr()

    parser = argparse.ArgumentParser(description="Agent benchmark runner")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--provider",
        default="mock",
        choices=["mock", "real"],
        help=(
            "'mock' (default, deterministic, no keys) or 'real' (requires "
            "a configured GEMINI/OPENAI/DEEPSEEK key; opt-in for nightly runs)."
        ),
    )
    args = parser.parse_args(argv)

    if args.provider == "real":
        if not any(os.environ.get(k, "").strip() for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY")):
            # Emit an empty-but-parseable result so the nightly workflow
            # can still render a row instead of crashing the step.
            body = AgentBenchmarkResult(provider="real", requires_key=True).to_json()
            body["skipped"] = "no API key configured"
            print(json.dumps(body, indent=2, default=str))
            return 0

    import tempfile

    import app.models  # noqa: F401
    from _pytest.monkeypatch import MonkeyPatch
    from app.db import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    ws = Path(tempfile.mkdtemp(prefix="agent_bench_"))
    mp = MonkeyPatch()
    try:
        result = run_agent_benchmark(
            db=db,
            workspace=ws,
            monkeypatch=mp,
            provider=args.provider,
        )
    finally:
        mp.undo()
        db.close()

    body = result.to_json()
    if args.json:
        print(json.dumps(body, indent=2, default=str))
    else:
        print(
            f"agent_benchmark  provider={body['provider']}  "
            f"uplift={body['uplift']:.3f}  "
            f"with={body['average_with_memory']:.3f}  without={body['average_without_memory']:.3f}  "
            f"gate={'PASS' if body['passes_exit_gate'] else 'FAIL'}"
        )
        for s in body["scenarios"]:
            print(
                f"  {s['slug']:<22}  uplift={s['uplift']:+.2f}  "
                f"off={s['without_memory']['aggregate_score']:.2f}  "
                f"on={s['with_memory']['aggregate_score']:.2f}"
            )
    return 0 if result.passes_exit_gate else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())
