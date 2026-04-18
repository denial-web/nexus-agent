"""`nexus` CLI — chat, one-shot agent runs, feedback, and approvals listing."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx


def _base_url() -> str:
    return os.environ.get("NEXUS_URL", "http://127.0.0.1:9000").rstrip("/")


def _headers() -> dict[str, str]:
    key = os.environ.get("NEXUS_API_KEY", "").strip()
    h: dict[str, str] = {"Content-Type": "application/json"}
    if key:
        h["X-API-Key"] = key
    return h


def cmd_chat(_args: argparse.Namespace) -> int:
    print("Nexus agent chat (Ctrl+D / empty line twice to exit). API:", _base_url())
    lines: list[str] = []
    while True:
        try:
            line = input("> ")
        except EOFError:
            break
        if line.strip() == "" and lines:
            break
        lines.append(line)
    prompt = "\n".join(lines).strip()
    if not prompt:
        print("No input.")
        return 0
    return _run_agent_prompt(prompt)


def cmd_run(args: argparse.Namespace) -> int:
    return _run_agent_prompt(args.task)


def _run_agent_prompt(prompt: str) -> int:
    url = f"{_base_url()}/v1/agent/agent/run"
    body: dict[str, Any] = {"prompt": prompt}
    try:
        r = httpx.post(url, json=body, headers=_headers(), timeout=600.0)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        print("Request failed:", exc, file=sys.stderr)
        return 1
    print(json.dumps(data, indent=2))
    return 0 if data.get("status") == "completed" else 1


def cmd_feedback(args: argparse.Namespace) -> int:
    url = f"{_base_url()}/v1/agent/agent/feedback"
    try:
        r = httpx.post(
            url,
            json={"trace_id": args.trace_id, "feedback": args.feedback},
            headers=_headers(),
            timeout=60.0,
        )
        r.raise_for_status()
        print(r.json())
    except Exception as exc:
        print("Request failed:", exc, file=sys.stderr)
        return 1
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    url = f"{_base_url()}/v1/governance/approve/{args.request_id}"
    body: dict[str, Any] = {"approver_id": args.approver or "cli-user", "decision": "approve"}
    try:
        r = httpx.post(url, json=body, headers=_headers(), timeout=60.0)
        r.raise_for_status()
        print(json.dumps(r.json(), indent=2))
    except Exception as exc:
        print("Request failed:", exc, file=sys.stderr)
        return 1
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    url = f"{_base_url()}/v1/agent/agent/resume"
    try:
        r = httpx.post(url, json={"trace_id": args.trace_id}, headers=_headers(), timeout=600.0)
        r.raise_for_status()
        print(json.dumps(r.json(), indent=2))
    except Exception as exc:
        print("Request failed:", exc, file=sys.stderr)
        return 1
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    url = f"{_base_url()}/v1/governance/approvals"
    try:
        r = httpx.get(url, headers=_headers(), timeout=30.0)
        r.raise_for_status()
        data = r.json()
        pending = [a for a in data if a.get("status") == "pending"]
        if pending:
            print(f"{len(pending)} pending approval(s):")
            for a in pending:
                print(f"  {a.get('id')} — {a.get('action_type')} (risk: {a.get('risk_level')})")
        else:
            print("No pending approvals.")
    except Exception as exc:
        print("Could not fetch status:", exc, file=sys.stderr)
        return 1
    return 0


def cmd_skills_list(args: argparse.Namespace) -> int:
    params = "?enabled_only=false" if args.all else "?enabled_only=true"
    url = f"{_base_url()}/v1/skills{params}"
    try:
        r = httpx.get(url, headers=_headers(), timeout=30.0)
        r.raise_for_status()
        skills = r.json()
    except Exception as exc:
        print("Request failed:", exc, file=sys.stderr)
        return 1
    if not skills:
        print("No skills found.")
        return 0
    for s in skills:
        flag = " [FLAGGED]" if s.get("flagged") else ""
        enabled = "on" if s.get("enabled") else "off"
        avg = f"{s['avg_reward']:.2f}" if s.get("avg_reward") is not None else "n/a"
        print(
            f"  {s['id'][:12]}  {s['name']:<30}  "
            f"avg={avg}  runs={s.get('total_runs', 0)}  "
            f"steps={s.get('step_count', 0)}  [{enabled}]{flag}"
        )
    return 0


def cmd_skills_execute(args: argparse.Namespace) -> int:
    url = f"{_base_url()}/v1/skills/{args.skill_id}/execute"
    try:
        r = httpx.post(url, headers=_headers(), timeout=600.0)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.json().get("detail", str(exc))
        print(f"Error: {detail}", file=sys.stderr)
        return 1
    except Exception as exc:
        print("Request failed:", exc, file=sys.stderr)
        return 1
    status = "SUCCESS" if data.get("success") else "FAILED"
    print(f"[{status}] Skill '{data.get('skill_name')}' — reward: {data.get('reward')}")
    for step in data.get("results", []):
        print(f"  Step {step.get('step', '?')}: {json.dumps(step, default=str)}")
    return 0 if data.get("success") else 1


def cmd_skills_import(args: argparse.Namespace) -> int:
    from app.core.agent.clawhub_import import import_skill_from_path, import_skill_from_url
    from app.db import SessionLocal

    if args.from_url:
        from app.config import settings as app_settings

        if app_settings.LOCAL_ONLY:
            print("URL import disabled when LOCAL_ONLY is set.", file=sys.stderr)
            return 1
        db = SessionLocal()
        try:
            sid = import_skill_from_url(args.from_url, db, force=args.force)
        finally:
            db.close()
        if not sid:
            print("Import failed or blocked.", file=sys.stderr)
            return 1
        print(sid)
        return 0
    if not args.path:
        print("Provide a path to SKILL.md or --from-url", file=sys.stderr)
        return 1
    db = SessionLocal()
    try:
        sid = import_skill_from_path(Path(args.path), db, force=args.force)
    finally:
        db.close()
    if not sid:
        print("Import failed or blocked.", file=sys.stderr)
        return 1
    print(sid)
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    from app.core.immune.benchmark import AttackCategory, run_benchmark

    cats = args.categories.split(",") if args.categories else None
    if cats:
        valid = {c.value for c in AttackCategory}
        bad = [c for c in cats if c not in valid]
        if bad:
            print(f"Unknown categories: {', '.join(bad)}", file=sys.stderr)
            print(f"Valid: {', '.join(sorted(valid))}", file=sys.stderr)
            return 1

    report = run_benchmark(categories=cats)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 0 if report.total_failed == 0 else 1

    print(f"\n{'=' * 60}")
    print(" Nexus Security Benchmark")
    print(f"{'=' * 60}")
    print(f" Timestamp:  {report.timestamp}")
    print(f" Duration:   {report.duration_ms:.1f}ms")
    print(f" Score:      {report.composite_score:.1%}")
    print(f" Payloads:   {report.total_passed}/{report.total_payloads} passed")
    print(f"{'─' * 60}")
    for cat in report.categories:
        status = "PASS" if cat.failed == 0 else "FAIL"
        bar_len = 20
        filled = int(cat.detection_rate * bar_len)
        bar = "\u2588" * filled + "\u2591" * (bar_len - filled)
        print(f" [{status:4s}] {cat.category:<25s} {bar} {cat.detection_rate:6.1%}  ({cat.passed}/{cat.total})")
        if cat.payloads_failed:
            for label in cat.payloads_failed:
                print(f"          - {label}")
    print(f"{'=' * 60}")

    if report.total_failed == 0:
        print(" Result: ALL CLEAR")
    else:
        print(f" Result: {report.total_failed} FAILURE(S)")

    if args.threshold:
        threshold = float(args.threshold)
        if report.composite_score < threshold:
            print(f" GATE FAILED: score {report.composite_score:.1%} < threshold {threshold:.1%}")
            return 1
        print(f" GATE PASSED: score {report.composite_score:.1%} >= threshold {threshold:.1%}")

    print()
    return 0 if report.total_failed == 0 else 1


def cmd_mcp_serve(_args: argparse.Namespace) -> int:
    from app.config import settings as app_settings

    if app_settings.LOCAL_ONLY:
        print("MCP stdio server disabled in LOCAL_ONLY mode.", file=sys.stderr)
        return 1
    from app.core.mcp.server import run_stdio_async

    asyncio.run(run_stdio_async())
    return 0


def cmd_mcp_backends(_args: argparse.Namespace) -> int:
    url = f"{_base_url()}/v1/mcp/backends"
    try:
        r = httpx.get(url, headers=_headers(), timeout=30.0)
        r.raise_for_status()
        print(json.dumps(r.json(), indent=2))
    except Exception as exc:
        print("Request failed:", exc, file=sys.stderr)
        return 1
    return 0


def cmd_mcp_add(args: argparse.Namespace) -> int:
    url = f"{_base_url()}/v1/mcp/backends"
    body = {
        "name": args.name,
        "url": args.backend_url,
        "transport": "streamable_http",
        "enabled": True,
    }
    try:
        r = httpx.post(url, json=body, headers=_headers(), timeout=30.0)
        r.raise_for_status()
        print(json.dumps(r.json(), indent=2))
    except httpx.HTTPStatusError as exc:
        print(exc.response.json().get("detail", str(exc)), file=sys.stderr)
        return 1
    except Exception as exc:
        print("Request failed:", exc, file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# `nexus memory` command group (Phase 12B Week 4).
#
# Six subcommands, each a thin wrapper around a REST endpoint we shipped in
# Week 2 + Week 4. No inline DB access — everything goes through the HTTP
# API so the CLI runs against any Nexus deployment (local, staging, prod)
# identically, and Covernor gates are applied consistently.
#
# `remember` (direct belief write) is intentionally deferred until we add
# a POST write endpoint; today beliefs are only created by the agent loop
# via the extractor + skepticism pipeline. Forcing writes to round-trip
# through governance in a future iteration keeps the zero-trust story intact.
# ---------------------------------------------------------------------------


def _extract_api_error(exc: httpx.HTTPStatusError) -> str:
    """Pull the structured error envelope out of a failed Nexus API response.

    The project's error middleware guarantees `error.code` / `error.message`
    on every non-2xx JSON body. Fall back to raw text so CLI operators
    still see *something* if a reverse proxy munged the response.
    """
    try:
        body = exc.response.json()
    except ValueError:
        return exc.response.text.strip() or str(exc)
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        code = err.get("code", "error")
        msg = err.get("message", "")
        return f"{code}: {msg}".rstrip(": ")
    return body.get("detail") if isinstance(body, dict) else str(exc)


def _memory_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """GET a Nexus memory endpoint; print a diagnostic and return None on failure."""
    url = f"{_base_url()}/v1/memory{path}"
    try:
        r = httpx.get(url, headers=_headers(), params=params, timeout=60.0)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as exc:
        print(f"Error: {_extract_api_error(exc)}", file=sys.stderr)
        return None
    except Exception as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return None


def _memory_post(path: str, body: dict[str, Any]) -> dict[str, Any] | None:
    url = f"{_base_url()}/v1/memory{path}"
    try:
        r = httpx.post(url, headers=_headers(), json=body, timeout=60.0)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as exc:
        print(f"Error: {_extract_api_error(exc)}", file=sys.stderr)
        return None
    except Exception as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return None


def cmd_memory_recall(args: argparse.Namespace) -> int:
    params: dict[str, Any] = {"limit": args.limit}
    if args.user:
        params["user_id"] = args.user
    if args.entity:
        params["entity"] = args.entity
    if args.predicate:
        params["predicate"] = args.predicate
    if args.include_tombstoned:
        params["include_tombstoned"] = "true"

    data = _memory_get("", params=params)
    if data is None:
        return 1
    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0
    beliefs = data.get("beliefs", [])
    if not beliefs:
        print("No beliefs matched.")
        return 0
    print(f"  {data['total']} total, showing {len(beliefs)}")
    for b in beliefs:
        scope = b.get("user_id") or "<shared>"
        status = "tomb" if not b.get("is_current") else "live"
        mean = f"{b.get('mean', 0):.2f}"
        print(
            f"  {b['id'][:12]}  {b['entity'][:20]:<20}  "
            f"{b['predicate'][:20]:<20}  "
            f"val={json.dumps(b['value'], default=str)[:30]:<30}  "
            f"p={mean}  {scope[:14]:<14}  [{status}]"
        )
    return 0


def cmd_memory_history(args: argparse.Namespace) -> int:
    data = _memory_get(f"/{args.belief_id}/history")
    if data is None:
        return 1
    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0
    versions = data.get("versions", [])
    print(f"  {data['entity']} · {data['predicate']}  ({len(versions)} versions)")
    for v in versions:
        observed = v.get("observed_at", "").replace("T", " ")[:19]
        superseded = (v.get("superseded_at") or "").replace("T", " ")[:19]
        tail = f"superseded {superseded}" if superseded else "LIVE"
        mean = f"{v.get('mean', 0):.2f}"
        print(
            f"  {v['id'][:12]}  observed {observed}  "
            f"val={json.dumps(v['value'], default=str)[:30]:<30}  "
            f"p={mean}  {tail}"
        )
    return 0


def cmd_memory_explain(args: argparse.Namespace) -> int:
    params: dict[str, Any] = {"query_text": args.query or ""}
    data = _memory_get(f"/{args.belief_id}/explain", params=params)
    if data is None:
        return 1
    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0
    b = data.get("belief", {})
    print(f"  {b.get('id', '')[:12]}  {b.get('entity', '')} · {b.get('predicate', '')}")
    print(f"  query_text   : {data.get('query_text') or '<empty>'}")
    print(f"  rank_in_scope: {data.get('rank_in_scope')}")
    print(f"  rrf_score    : {data.get('rrf_score'):.4f}")
    signals = data.get("signals", [])
    if signals:
        print("  signals:")
        for s in signals:
            print(f"    - {s['signal']:<20}  {s['score']:.4f}")
    return 0


def cmd_memory_forget(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {"entity": args.entity}
    if args.predicate:
        body["predicate"] = args.predicate
    if args.user:
        body["user_id"] = args.user
    data = _memory_post("/forget", body)
    if data is None:
        return 1
    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0
    print(
        f"Tombstoned {data['tombstoned']} belief(s) "
        f"for entity={data['entity']!r}"
        + (f" predicate={data['predicate']!r}" if data.get("predicate") else "")
        + (f" user_id={data['user_id']!r}" if data.get("user_id") else "")
    )
    return 0


def cmd_memory_stats(args: argparse.Namespace) -> int:
    data = _memory_get("/stats")
    if data is None:
        return 1
    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0
    print(f"  enabled          : {data.get('enabled')}")
    print(f"  total_live       : {data.get('total_live')}")
    print(f"  total_tombstoned : {data.get('total_tombstoned')}")
    by_ent = data.get("by_entity_type") or {}
    if by_ent:
        print("  by_entity_type   :")
        for k, v in sorted(by_ent.items()):
            print(f"    {k:<14} {v}")
    by_src = data.get("by_source_type") or {}
    if by_src:
        print("  by_source_type   :")
        for k, v in sorted(by_src.items()):
            print(f"    {k:<14} {v}")
    decay = data.get("decay_profile") or {}
    if decay:
        print("  decay_profile    :")
        for k, v in sorted(decay.items()):
            print(f"    {k:<14} {v}")
    return 0


def cmd_memory_verify(args: argparse.Namespace) -> int:
    """Run the hash-chain verifier against a live Nexus deployment.

    This is the showcase command: a single shell invocation that proves
    the tamper-evident audit trail claim. By default we pretty-print a
    short report; `--json` returns the raw API response for automation.

    Scope resolution mirrors `GET /v1/memory/integrity` semantics so
    there's exactly one mental model across CLI, dashboard, and API:
      `--user alice`              → Alice's chain only
      `--scope-null`              → NULL-user (shared/system) chain only
      (neither)                   → audit every chain
    """
    params: dict[str, Any] = {}
    if args.user:
        params["user_id"] = args.user
    elif args.scope_null:
        params["scope_all"] = "false"
    if args.as_of:
        params["as_of"] = args.as_of

    data = _memory_get("/integrity", params=params)
    if data is None:
        return 1
    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0 if data.get("verified") else 2

    verified = bool(data.get("verified"))
    rows = data.get("rows_checked", 0)
    chains = data.get("checked_user_count", 0)

    print(f"\n{'=' * 60}")
    print(" Nexus Memory Integrity")
    print(f"{'=' * 60}")
    print(f" Status       : {'VERIFIED' if verified else 'BROKEN'}")
    print(f" Rows checked : {rows}")
    print(f" Chains walked: {chains}")
    if data.get("as_of"):
        print(f" As of        : {data['as_of']}")
    if not verified:
        print(f" First break  : {data.get('first_break_at')}")
        print(f" Reason       : {data.get('reason')}")
    print(f"{'=' * 60}\n")
    # Exit 2 for "chain intact but broken" so `&&` chains fail loud, distinct
    # from 1 (transport error) so operators can branch on cause.
    return 0 if verified else 2


def cmd_skills_toggle(args: argparse.Namespace) -> int:
    enabled = args.state.lower() in ("on", "true", "enable", "1")
    url = f"{_base_url()}/v1/skills/{args.skill_id}"
    try:
        r = httpx.patch(url, json={"enabled": enabled}, headers=_headers(), timeout=30.0)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.json().get("detail", str(exc))
        print(f"Error: {detail}", file=sys.stderr)
        return 1
    except Exception as exc:
        print("Request failed:", exc, file=sys.stderr)
        return 1
    state = "enabled" if data.get("enabled") else "disabled"
    print(f"Skill '{data.get('name')}' is now {state}.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="nexus", description="Nexus Agent CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_chat = sub.add_parser("chat", help="Interactive agent chat (stdin)")
    p_chat.set_defaults(fn=cmd_chat)

    p_run = sub.add_parser("run", help="One-shot agent task")
    p_run.add_argument("task", help="Task description")
    p_run.set_defaults(fn=cmd_run)

    p_fb = sub.add_parser("feedback", help="Rate a trace good/bad")
    p_fb.add_argument("trace_id")
    p_fb.add_argument("feedback", choices=["good", "bad"])
    p_fb.set_defaults(fn=cmd_feedback)

    p_ap = sub.add_parser("approve", help="Approve a pending action")
    p_ap.add_argument("request_id")
    p_ap.add_argument("--approver", default=None, help="Approver ID (default: cli-user)")
    p_ap.set_defaults(fn=cmd_approve)

    p_rs = sub.add_parser("resume", help="Resume agent after approval")
    p_rs.add_argument("trace_id")
    p_rs.set_defaults(fn=cmd_resume)

    p_st = sub.add_parser("status", help="Show pending approvals")
    p_st.set_defaults(fn=cmd_status)

    p_bench = sub.add_parser("benchmark", help="Run security benchmark against the immune scanner")
    p_bench.add_argument("--json", action="store_true", help="Output JSON report")
    p_bench.add_argument("--categories", default=None, help="Comma-separated category filter")
    p_bench.add_argument("--threshold", default=None, help="Minimum score (0.0-1.0) for CI gate pass")
    p_bench.set_defaults(fn=cmd_benchmark)

    p_sk = sub.add_parser("skills", help="Manage reusable skills")
    sk_sub = p_sk.add_subparsers(dest="skills_cmd", required=True)

    p_sk_list = sk_sub.add_parser("list", help="List skills")
    p_sk_list.add_argument("--all", action="store_true", help="Include disabled skills")
    p_sk_list.set_defaults(fn=cmd_skills_list)

    p_sk_exec = sk_sub.add_parser("execute", help="Execute a skill by ID")
    p_sk_exec.add_argument("skill_id")
    p_sk_exec.set_defaults(fn=cmd_skills_execute)

    p_sk_toggle = sk_sub.add_parser("toggle", help="Enable or disable a skill")
    p_sk_toggle.add_argument("skill_id")
    p_sk_toggle.add_argument("state", choices=["on", "off"], help="on to enable, off to disable")
    p_sk_toggle.set_defaults(fn=cmd_skills_toggle)

    p_sk_imp = sk_sub.add_parser("import", help="Import SKILL.md into Nexus")
    p_sk_imp.add_argument("path", nargs="?", help="Path to SKILL.md")
    p_sk_imp.add_argument("--from-url", dest="from_url", default=None, help="Fetch SKILL.md from URL")
    p_sk_imp.add_argument("--force", action="store_true", help="Re-import even if hash matches")
    p_sk_imp.set_defaults(fn=cmd_skills_import)

    p_mcp = sub.add_parser("mcp", help="MCP governance proxy")
    mcp_sub = p_mcp.add_subparsers(dest="mcp_cmd", required=True)

    p_mcp_serve = mcp_sub.add_parser("serve", help="Run MCP proxy on stdio (for MCP clients)")
    p_mcp_serve.set_defaults(fn=cmd_mcp_serve)

    p_mcp_back = mcp_sub.add_parser("backends", help="List MCP backends via API")
    p_mcp_back.set_defaults(fn=cmd_mcp_backends)

    p_mcp_add = mcp_sub.add_parser("add", help="Register an MCP backend via API")
    p_mcp_add.add_argument("name", help="Backend name")
    p_mcp_add.add_argument("backend_url", help="MCP server URL (e.g. http://127.0.0.1:3333/mcp)")
    p_mcp_add.set_defaults(fn=cmd_mcp_add)

    p_mem = sub.add_parser("memory", help="Inspect + govern the belief memory subsystem")
    mem_sub = p_mem.add_subparsers(dest="memory_cmd", required=True)

    p_mem_recall = mem_sub.add_parser("recall", help="List current beliefs")
    p_mem_recall.add_argument("--user", default=None, help="Filter by user_id")
    p_mem_recall.add_argument("--entity", default=None, help="Exact entity match")
    p_mem_recall.add_argument("--predicate", default=None, help="Exact predicate match")
    p_mem_recall.add_argument("--limit", type=int, default=50, help="Max rows (1-500)")
    p_mem_recall.add_argument(
        "--include-tombstoned",
        action="store_true",
        help="Include superseded rows",
    )
    p_mem_recall.add_argument("--json", action="store_true", help="Emit raw API JSON")
    p_mem_recall.set_defaults(fn=cmd_memory_recall)

    p_mem_hist = mem_sub.add_parser("history", help="Show bitemporal version history for a belief")
    p_mem_hist.add_argument("belief_id", help="Belief id to trace (any version in the chain)")
    p_mem_hist.add_argument("--json", action="store_true", help="Emit raw API JSON")
    p_mem_hist.set_defaults(fn=cmd_memory_history)

    p_mem_exp = mem_sub.add_parser("explain", help="Show per-signal retrieval ranking for a belief")
    p_mem_exp.add_argument("belief_id")
    p_mem_exp.add_argument(
        "--query",
        default="",
        help="Query context for ranking (empty → confidence-only)",
    )
    p_mem_exp.add_argument("--json", action="store_true", help="Emit raw API JSON")
    p_mem_exp.set_defaults(fn=cmd_memory_explain)

    p_mem_forget = mem_sub.add_parser("forget", help="Tombstone beliefs (user-directed erasure)")
    p_mem_forget.add_argument("entity", help="Entity to tombstone (required)")
    p_mem_forget.add_argument(
        "--predicate",
        default=None,
        help="Tombstone only this predicate (default: all predicates on the entity)",
    )
    p_mem_forget.add_argument("--user", default=None, help="Restrict to a specific user scope")
    p_mem_forget.add_argument("--json", action="store_true", help="Emit raw API JSON")
    p_mem_forget.set_defaults(fn=cmd_memory_forget)

    p_mem_stats = mem_sub.add_parser("stats", help="Show subsystem counts + decay profile")
    p_mem_stats.add_argument("--json", action="store_true", help="Emit raw API JSON")
    p_mem_stats.set_defaults(fn=cmd_memory_stats)

    p_mem_ver = mem_sub.add_parser(
        "verify",
        help="Verify the tamper-evident belief hash chain",
    )
    p_mem_ver.add_argument("--user", default=None, help="Verify one user's chain only")
    p_mem_ver.add_argument(
        "--scope-null",
        action="store_true",
        help="Verify only the NULL-user (shared/system) chain",
    )
    p_mem_ver.add_argument(
        "--as-of",
        default=None,
        help="Bitemporal cutoff (ISO 8601 with offset, e.g. 2026-04-17T00:00:00+00:00)",
    )
    p_mem_ver.add_argument("--json", action="store_true", help="Emit raw API JSON")
    p_mem_ver.set_defaults(fn=cmd_memory_verify)

    args = parser.parse_args()
    fn = getattr(args, "fn", None)
    if fn is None:
        parser.print_help()
        raise SystemExit(1)
    raise SystemExit(fn(args))


if __name__ == "__main__":
    main()
