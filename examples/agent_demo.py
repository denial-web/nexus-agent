"""
Digital Employee demo — the full reflection + learning loop.

Showcases:
  1. Agent runs a multi-step task (tool calls governed by Covernor)
  2. A tool gets DENIED → agent adapts and tries a different approach
  3. Task completes → reward score computed
  4. User gives feedback ("good") → stored on the trace
  5. A similar task → agent retrieves past episode from memory

Works in mock mode (no LLM keys needed). Start the server first:

    make dev
    # or: uvicorn app.main:app --reload --port 9000

Then:

    python examples/agent_demo.py

"""

import json
import os
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("NEXUS_URL", "http://localhost:9000")
API_KEY = os.environ.get("NEXUS_API_KEY", "")

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"


def api(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=data, headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def banner(text: str) -> None:
    print(f"\n{BOLD}{CYAN}{'─' * 60}{RESET}")
    print(f"{BOLD}{CYAN}  {text}{RESET}")
    print(f"{BOLD}{CYAN}{'─' * 60}{RESET}\n")


def step(n: int, text: str) -> None:
    print(f"  {BOLD}[Step {n}]{RESET} {text}")


def result_line(key: str, value: str, color: str = "") -> None:
    print(f"    {DIM}{key}:{RESET} {color}{value}{RESET}")


def ensure_deny_policy() -> None:
    """Create a policy that denies sudo shell commands (may already exist from seeding)."""
    api("POST", "/api/governance/policies", {
        "name": "demo-deny-sudo",
        "action_pattern": "shell_exec",
        "resource_pattern": "*sudo*",
        "decision": "deny",
        "risk_level": "high",
        "required_approvals": 0,
        "priority": 1,
    })


def main() -> None:
    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}  NEXUS AGENT — Digital Employee Demo{RESET}")
    print(f"{BOLD}  The AI agent that works, learns, and can be trusted to.{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")
    print(f"\n{DIM}Server: {BASE_URL} | Mode: mock (no LLM keys needed){RESET}")

    # ── Preparation ─────────────────────────────────────────────
    banner("Preparation: ensure denial policy exists")
    ensure_deny_policy()
    print(f"  {GREEN}✓{RESET} sudo commands will be denied by Covernor")

    # ── Task 1: Agent runs, gets denied, adapts ─────────────────
    banner("Task 1: 'Read the README and summarize it'")
    print("  The agent will plan steps, use tools, and self-correct.\n")

    t0 = time.time()
    r = api("POST", "/api/agent/agent/run", {
        "prompt": "Read the README.md file and give a one-paragraph summary.",
    })
    elapsed = time.time() - t0

    status = r.get("status", "unknown")
    color = GREEN if status == "completed" else RED if status in ("blocked", "halted", "error") else YELLOW

    step(1, "Agent executed")
    result_line("Status", status, color)
    result_line("Trace ID", r.get("trace_id", "N/A"))
    result_line("Steps taken", str(r.get("total_steps", "?")))
    result_line("Self-corrections", str(r.get("self_corrections", 0)))
    result_line("Latency", f"{elapsed:.1f}s")

    reward = r.get("task_reward_score")
    if reward is not None:
        rc = GREEN if reward > 0.7 else YELLOW if reward > 0.4 else RED
        result_line("Task reward", f"{reward:.2f}", rc)

    trajectory = r.get("trajectory", [])
    if trajectory:
        print(f"\n  {BOLD}Trajectory:{RESET}")
        for i, t in enumerate(trajectory):
            kind = t.get("kind", "?")
            if kind == "tool":
                tool = t.get("tool", "?")
                ok = t.get("success", False)
                sym = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
                refl = t.get("reflection", "")
                print(f"    {i + 1}. {sym} {BOLD}{tool}{RESET}")
                if refl and refl != "Step succeeded; proceeding.":
                    print(f"       {DIM}Reflection: {refl[:120]}{RESET}")
            elif kind == "final":
                content = t.get("content", "")[:200]
                print(f"    {i + 1}. {GREEN}→ Final answer{RESET}: {content}...")

    if r.get("response"):
        print(f"\n  {BOLD}Response (first 300 chars):{RESET}")
        print(f"    {r['response'][:300]}")

    trace_id = r.get("trace_id")

    # ── Feedback ────────────────────────────────────────────────
    if trace_id and status == "completed":
        banner("Feedback: user rates the result")
        step(2, "Sending 'good' feedback on trace")
        fb = api("POST", "/api/agent/agent/feedback", {
            "trace_id": trace_id,
            "feedback": "good",
        })
        result_line("Feedback recorded", fb.get("user_feedback", "?"), GREEN)
        print(f"\n  {DIM}This feeds the reward score and episodic memory.{RESET}")
        print(f"  {DIM}Next time a similar task comes in, the agent recalls this success.{RESET}")

    # ── Task 2: similar task, agent should recall ───────────────
    banner("Task 2: Similar task — does the agent remember?")
    print("  Running a related task to see if episodic memory is injected.\n")

    r2 = api("POST", "/api/agent/agent/run", {
        "prompt": "Read the README.md and list the main features.",
    })

    status2 = r2.get("status", "unknown")
    color2 = GREEN if status2 == "completed" else RED
    step(3, "Second agent run")
    result_line("Status", status2, color2)
    result_line("Steps taken", str(r2.get("total_steps", "?")))

    reward2 = r2.get("task_reward_score")
    if reward2 is not None:
        rc2 = GREEN if reward2 > 0.7 else YELLOW
        result_line("Task reward", f"{reward2:.2f}", rc2)

    # ── Summary ─────────────────────────────────────────────────
    banner("Summary")
    print(f"  {BOLD}What just happened:{RESET}")
    print("  1. Agent planned steps, called tools (all governed by Covernor)")
    print("  2. Every tool call was policy-checked (default-deny)")
    print("  3. After each step: LLM reflection + critic evaluation")
    print("  4. Task reward computed from critic scores + completion + efficiency")
    print("  5. User feedback ('good') stored on trace and episode")
    print("  6. Second task: agent retrieved past episode to guide planning")
    print("  7. Full audit trail: step traces, hash-chained trace, episode record")

    print(f"\n  {DIM}Explore more:{RESET}")
    print(f"    Dashboard:  {BASE_URL}/dashboard")
    print(f"    Trace:      {BASE_URL}/dashboard/trace/{trace_id}" if trace_id else "")
    print("    CLI:        nexus run \"your task here\"")
    print(f"    Feedback:   nexus feedback {trace_id} good" if trace_id else "")

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}  Nexus Agent: works, learns, and can be trusted to.{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}\n")


if __name__ == "__main__":
    main()
