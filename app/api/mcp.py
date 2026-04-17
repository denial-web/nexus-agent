"""MCP backend registry API."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.core.covernor.policy_engine import evaluate_action
from app.core.mcp.config import McpBackend, load_backends, save_backends
from app.db import get_db
from app.sanitize import sanitize_for_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp", tags=["MCP"])


class McpBackendCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    url: str = Field("", max_length=2000)
    transport: str = "streamable_http"
    command: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class McpBackendPatch(BaseModel):
    enabled: bool | None = None
    url: str | None = None


def _file_path() -> str:
    return settings.MCP_BACKENDS_FILE


@router.get("/backends")
def list_mcp_backends() -> list[dict[str, Any]]:
    """List configured MCP backends."""
    backends = load_backends(_file_path())
    out: list[dict[str, Any]] = []
    for b in backends:
        out.append(
            {
                "name": b.name,
                "url": b.url,
                "transport": b.transport,
                "command": b.command,
                "env": b.env,
                "enabled": b.enabled,
            }
        )
    return out


@router.post("/backends")
def create_mcp_backend(body: McpBackendCreate) -> dict[str, Any]:
    if settings.LOCAL_ONLY:
        raise HTTPException(status_code=503, detail="MCP backends cannot be modified in LOCAL_ONLY mode")
    backends = load_backends(_file_path())
    if any(x.name == body.name for x in backends):
        raise HTTPException(status_code=400, detail=f"Backend {sanitize_for_error(body.name)} already exists")
    backends.append(
        McpBackend(
            name=body.name,
            url=body.url,
            transport=body.transport,
            command=body.command,
            env=dict(body.env),
            enabled=body.enabled,
        )
    )
    save_backends(_file_path(), backends)
    return {"name": body.name, "ok": True}


@router.patch("/backends/{name}")
def patch_mcp_backend(
    name: str,
    body: McpBackendPatch,
) -> dict[str, Any]:
    if settings.LOCAL_ONLY:
        raise HTTPException(status_code=503, detail="MCP backends cannot be modified in LOCAL_ONLY mode")
    backends = load_backends(_file_path())
    found = None
    for b in backends:
        if b.name == name:
            found = b
            break
    if not found:
        raise HTTPException(status_code=404, detail="Backend not found")
    if body.enabled is not None:
        found.enabled = body.enabled
    if body.url is not None:
        found.url = body.url
    save_backends(_file_path(), backends)
    return {"name": name, "enabled": found.enabled, "url": found.url}


@router.delete("/backends/{name}")
def delete_mcp_backend(name: str) -> dict[str, Any]:
    if settings.LOCAL_ONLY:
        raise HTTPException(status_code=503, detail="MCP backends cannot be modified in LOCAL_ONLY mode")
    backends = load_backends(_file_path())
    n = len(backends)
    backends = [b for b in backends if b.name != name]
    if len(backends) == n:
        raise HTTPException(status_code=404, detail="Backend not found")
    save_backends(_file_path(), backends)
    return {"deleted": name}


@router.get("/backends/{name}/tools")
async def list_backend_tools(name: str, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """List tools from a backend with governance annotations (best-effort)."""
    if settings.LOCAL_ONLY:
        raise HTTPException(status_code=503, detail="MCP unavailable in LOCAL_ONLY mode")
    backends = load_backends(_file_path())
    backend = next((b for b in backends if b.name == name), None)
    if not backend:
        raise HTTPException(status_code=404, detail="Backend not found")
    if not backend.url:
        raise HTTPException(status_code=400, detail="Backend has no URL")
    from fastmcp import Client

    from app.core.mcp.proxy import mcp_action_id

    try:
        async with Client(backend.url) as client:
            tools = await client.list_tools()
    except Exception as e:
        logger.warning("list_tools failed for %s", name, exc_info=True)
        raise HTTPException(status_code=502, detail="Could not reach MCP backend") from e

    out: list[dict[str, Any]] = []
    for t in tools:
        action = mcp_action_id(backend.name, t.name)
        gov = evaluate_action(action, "*", db_session=db)
        meta = dict(t.meta) if t.meta else {}
        meta["nexus"] = {
            "action": action,
            "decision": gov.decision,
            "policy_id": gov.policy_id,
            "policy_name": gov.policy_name,
        }
        out.append(
            {
                "name": t.name,
                "title": t.title,
                "description": t.description,
                "inputSchema": t.inputSchema,
                "meta": meta,
            }
        )
    return out
