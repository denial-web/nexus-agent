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
    url = f"{_base_url()}/api/agent/agent/run"
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
    url = f"{_base_url()}/api/agent/agent/feedback"
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
    url = f"{_base_url()}/api/governance/approve/{args.request_id}"
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
    url = f"{_base_url()}/api/agent/agent/resume"
    try:
        r = httpx.post(url, json={"trace_id": args.trace_id}, headers=_headers(), timeout=600.0)
        r.raise_for_status()
        print(json.dumps(r.json(), indent=2))
    except Exception as exc:
        print("Request failed:", exc, file=sys.stderr)
        return 1
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    url = f"{_base_url()}/api/governance/approvals"
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
    url = f"{_base_url()}/api/skills{params}"
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
    url = f"{_base_url()}/api/skills/{args.skill_id}/execute"
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


def cmd_mcp_serve(_args: argparse.Namespace) -> int:
    from app.config import settings as app_settings

    if app_settings.LOCAL_ONLY:
        print("MCP stdio server disabled in LOCAL_ONLY mode.", file=sys.stderr)
        return 1
    from app.core.mcp.server import run_stdio_async

    asyncio.run(run_stdio_async())
    return 0


def cmd_mcp_backends(_args: argparse.Namespace) -> int:
    url = f"{_base_url()}/api/mcp/backends"
    try:
        r = httpx.get(url, headers=_headers(), timeout=30.0)
        r.raise_for_status()
        print(json.dumps(r.json(), indent=2))
    except Exception as exc:
        print("Request failed:", exc, file=sys.stderr)
        return 1
    return 0


def cmd_mcp_add(args: argparse.Namespace) -> int:
    url = f"{_base_url()}/api/mcp/backends"
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


def cmd_skills_toggle(args: argparse.Namespace) -> int:
    enabled = args.state.lower() in ("on", "true", "enable", "1")
    url = f"{_base_url()}/api/skills/{args.skill_id}"
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

    args = parser.parse_args()
    fn = getattr(args, "fn", None)
    if fn is None:
        parser.print_help()
        raise SystemExit(1)
    raise SystemExit(fn(args))


if __name__ == "__main__":
    main()
