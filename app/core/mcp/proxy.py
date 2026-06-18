"""Governed MCP tool forwarding — immune scan, Covernor, optional traces."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from typing import Any

import anyio
import mcp.types
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context
from fastmcp.server.tasks.config import TaskConfig
from fastmcp.tools.tool import Tool, ToolResult
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.core.covernor.policy_engine import evaluate_action
from app.core.immune.scanner import is_tool_call_blocked, scan_input
from app.core.mcp.config import McpBackend, load_backends
from app.db import SessionLocal
from app.services.integrity import compute_trace_hash

logger = logging.getLogger(__name__)

MCP_SESSION_ID = "mcp-proxy"
MCP_PROXY_RUN_MODE = "mcp_proxy"
MCP_APPROVAL_JSONRPC_CODE = -32001


def mcp_action_id(backend_name: str, tool_name: str) -> str:
    return f"mcp:{backend_name}:{tool_name}"


def exposed_tool_name(backend_name: str, remote_tool_name: str) -> str:
    """Safe MCP tool name (alphanumeric + underscore)."""
    b = re.sub(r"[^a-zA-Z0-9_-]+", "_", backend_name)[:40]
    t = re.sub(r"[^a-zA-Z0-9_-]+", "_", remote_tool_name)[:80]
    return f"{b}__{t}"


def _governance_meta(backend_name: str, remote_tool_name: str, db: Session) -> dict[str, Any]:
    action = mcp_action_id(backend_name, remote_tool_name)
    gov = evaluate_action(action, "*", db_session=db)
    return {
        "nexus": {
            "action": action,
            "decision": gov.decision,
            "policy_id": gov.policy_id,
            "policy_name": gov.policy_name,
        }
    }


def _merge_meta(base: dict[str, Any] | None, backend_name: str, remote_tool_name: str) -> dict[str, Any]:
    out = dict(base) if base else {}
    db = SessionLocal()
    try:
        out.update(_governance_meta(backend_name, remote_tool_name, db))
    finally:
        db.close()
    return out


def _persist_mcp_trace(
    *,
    backend_name: str,
    remote_tool_name: str,
    prompt_payload: str,
    response_text: str | None,
    status: str,
    immune_verdict: str,
    immune_details: dict[str, Any] | None,
    governance_decision: str | None,
    governance_policy_id: str | None,
    error: str | None,
) -> None:
    db = SessionLocal()
    try:
        from app.models.trace import Trace

        seq = (db.query(func.max(Trace.sequence)).filter(Trace.session_id == MCP_SESSION_ID).scalar() or 0) + 1
        prev = db.query(Trace).filter_by(session_id=MCP_SESSION_ID).order_by(Trace.sequence.desc()).first()
        prev_hash = prev.trace_hash if prev and prev.trace_hash else "genesis"
        trace_id = uuid.uuid4().hex
        prompt_hash = hashlib.sha256(prompt_payload.encode()).hexdigest()
        rh = hashlib.sha256((response_text or "").encode()).hexdigest() if response_text else None
        th = compute_trace_hash(trace_id, prev_hash, prompt_hash, rh, status)
        row = Trace(
            id=trace_id,
            session_id=MCP_SESSION_ID,
            sequence=seq,
            prompt=prompt_payload[:50000],
            prompt_hash=prompt_hash,
            immune_verdict=immune_verdict,
            immune_score=None,
            immune_details=immune_details,
            status=status,
            response=response_text[:50000] if response_text else None,
            response_hash=rh,
            governance_status=governance_decision,
            governance_policy_id=governance_policy_id,
            error=error[:10000] if error else None,
            run_mode=MCP_PROXY_RUN_MODE,
            mcp_backend=backend_name,
            mcp_tool_name=remote_tool_name,
            prev_hash=prev_hash,
            trace_hash=th,
        )
        db.add(row)
        db.commit()
    except Exception:
        logger.exception("Failed to persist MCP trace")
        db.rollback()
    finally:
        db.close()


def _should_trace(*, allowed: bool, audited_all: bool) -> bool:
    if audited_all:
        return True
    return not allowed


class GovernedMcpTool(Tool):
    """Forwards to a remote MCP tool after immune scan + Covernor."""

    task_config: TaskConfig = TaskConfig(mode="forbidden")

    def __init__(
        self,
        *,
        backend: McpBackend,
        remote_tool: mcp.types.Tool,
        exposed_name: str,
    ) -> None:
        params = remote_tool.inputSchema
        if not params:
            params = {"type": "object", "properties": {}}
        meta_raw = remote_tool.meta if isinstance(remote_tool.meta, dict) else {}
        nexus_meta = _merge_meta(meta_raw, backend.name, remote_tool.name)
        super().__init__(
            name=exposed_name,
            title=remote_tool.title,
            description=remote_tool.description,
            parameters=params,
            output_schema=remote_tool.outputSchema,
            annotations=remote_tool.annotations,
            icons=remote_tool.icons,
            meta=nexus_meta,
            tags=set((meta_raw.get("_fastmcp", {}) or {}).get("tags", []) or []),
        )
        object.__setattr__(self, "_backend", backend)
        object.__setattr__(self, "_remote_tool_name", remote_tool.name)

    async def run(
        self,
        arguments: dict[str, Any],
        context: Context | None = None,
    ) -> ToolResult:
        backend: McpBackend = self._backend
        remote: str = self._remote_tool_name
        action = mcp_action_id(backend.name, remote)

        if settings.LOCAL_ONLY:
            msg = "MCP proxy disabled in LOCAL_ONLY mode"
            if _should_trace(allowed=False, audited_all=settings.MCP_AUDIT_ALL):
                _persist_mcp_trace(
                    backend_name=backend.name,
                    remote_tool_name=remote,
                    prompt_payload=json.dumps(
                        {"tool": remote, "arguments": arguments},
                        default=str,
                        ensure_ascii=False,
                    ),
                    response_text=None,
                    status="blocked",
                    immune_verdict="pass",
                    immune_details=None,
                    governance_decision="deny",
                    governance_policy_id=None,
                    error=msg,
                )
            raise ToolError(msg)

        # ensure_ascii=False so CJK/Cyrillic/Arabic injection text reaches
        # the scanner literally. Without it, `json.dumps` escapes non-ASCII
        # to `\uXXXX`, which silently bypasses all multi-language injection
        # patterns — a real hole surfaced by tests/eval/tool_injection_redteam.py.
        payload = json.dumps(
            {"backend": backend.name, "tool": remote, "arguments": arguments},
            default=str,
            ensure_ascii=False,
        )[:50000]
        # Tool-call payloads are serialized JSON: the surrounding quotes are
        # structural, not a use/mention, so keep the conservative verdict
        # (no quoted-data relaxation — see scan_input / is_tool_call_blocked).
        scan = scan_input(payload, treat_quoted_as_data=False)
        immune_verdict = scan.verdict.value
        immune_details = {
            "verdict": immune_verdict,
            "score": scan.score,
            "triggers": list(scan.triggers) if scan.triggers else [],
        }

        # Tool calls have no hardening fallback (see is_tool_call_blocked
        # docstring). FLAG at this boundary means "detected injection
        # signal, nothing will strip it" — reject.
        if is_tool_call_blocked(scan):
            if _should_trace(allowed=False, audited_all=settings.MCP_AUDIT_ALL):
                _persist_mcp_trace(
                    backend_name=backend.name,
                    remote_tool_name=remote,
                    prompt_payload=payload,
                    response_text=None,
                    status="blocked",
                    immune_verdict=immune_verdict,
                    immune_details=immune_details,
                    governance_decision=None,
                    governance_policy_id=None,
                    error="Immune scanner blocked MCP tool arguments",
                )
            raise ToolError("Immune scanner blocked MCP tool arguments")

        def _gov() -> Any:
            db = SessionLocal()
            try:
                return evaluate_action(action, payload[:2000], db_session=db)
            finally:
                db.close()

        gov = await anyio.to_thread.run_sync(_gov)

        if gov.decision == "deny":
            if _should_trace(allowed=False, audited_all=settings.MCP_AUDIT_ALL):
                _persist_mcp_trace(
                    backend_name=backend.name,
                    remote_tool_name=remote,
                    prompt_payload=payload,
                    response_text=None,
                    status="denied",
                    immune_verdict=immune_verdict,
                    immune_details=immune_details,
                    governance_decision="deny",
                    governance_policy_id=gov.policy_id,
                    error=gov.reason,
                )
            raise ToolError(f"Covernor denied: {gov.reason}")

        if gov.decision == "require_approval":
            msg = (
                f"Tool requires human approval — configure allow/deny for {action} "
                f"or use the Nexus dashboard (JSON-RPC {MCP_APPROVAL_JSONRPC_CODE})"
            )
            if _should_trace(allowed=False, audited_all=settings.MCP_AUDIT_ALL):
                _persist_mcp_trace(
                    backend_name=backend.name,
                    remote_tool_name=remote,
                    prompt_payload=payload,
                    response_text=None,
                    status="pending",
                    immune_verdict=immune_verdict,
                    immune_details=immune_details,
                    governance_decision="require_approval",
                    governance_policy_id=gov.policy_id,
                    error=msg,
                )
            raise ToolError(msg)

        if not backend.url:
            raise ToolError("Backend has no URL configured")

        async with Client(backend.url) as client:
            result = await client.call_tool(remote, arguments, raise_on_error=False)

        if getattr(result, "isError", False):
            err_text = ""
            if result.content:
                c0 = result.content[0]
                if hasattr(c0, "text"):
                    err_text = str(c0.text)
            if _should_trace(allowed=True, audited_all=settings.MCP_AUDIT_ALL):
                _persist_mcp_trace(
                    backend_name=backend.name,
                    remote_tool_name=remote,
                    prompt_payload=payload,
                    response_text=err_text,
                    status="error",
                    immune_verdict=immune_verdict,
                    immune_details=immune_details,
                    governance_decision="allow",
                    governance_policy_id=gov.policy_id,
                    error=err_text,
                )
            raise ToolError(err_text or "MCP tool returned an error")

        resp_text = None
        if result.content:
            parts: list[str] = []
            for block in result.content:
                if hasattr(block, "text"):
                    parts.append(str(block.text))
            resp_text = "\n".join(parts) if parts else None

        if settings.MCP_AUDIT_ALL:
            _persist_mcp_trace(
                backend_name=backend.name,
                remote_tool_name=remote,
                prompt_payload=payload,
                response_text=resp_text,
                status="completed",
                immune_verdict=immune_verdict,
                immune_details=immune_details,
                governance_decision="allow",
                governance_policy_id=gov.policy_id,
                error=None,
            )

        sc = getattr(result, "structuredContent", None)
        return ToolResult(
            content=result.content,
            structured_content=sc,
            meta=result.meta,
        )


async def register_backend_tools(server: Any) -> None:
    """Discover remote tools and register governed proxies (FastMCP lifespan)."""
    from fastmcp.server.server import FastMCP

    if not isinstance(server, FastMCP):
        return
    backends = load_backends(settings.MCP_BACKENDS_FILE)
    for b in backends:
        if not b.enabled or not b.url:
            continue
        if settings.LOCAL_ONLY:
            logger.info("Skipping MCP backend %s — LOCAL_ONLY", b.name)
            continue
        try:
            async with Client(b.url) as client:
                listed = await client.list_tools()
        except Exception:
            logger.warning("Could not list tools from MCP backend %s", b.name, exc_info=True)
            continue
        for rt in listed:
            exposed = exposed_tool_name(b.name, rt.name)
            server.add_tool(GovernedMcpTool(backend=b, remote_tool=rt, exposed_name=exposed))
