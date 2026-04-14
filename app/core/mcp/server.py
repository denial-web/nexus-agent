"""FastMCP HTTP + stdio entrypoints for the governed MCP proxy."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP

from app.config import settings
from app.core.mcp.proxy import register_backend_tools

if TYPE_CHECKING:
    from fastmcp.server.http import StarletteWithLifespan

logger = logging.getLogger(__name__)

_mcp_instance: FastMCP[Any] | None = None
_http_app: StarletteWithLifespan | None = None


@asynccontextmanager
async def _nexus_mcp_lifespan(server: FastMCP[Any]) -> AsyncGenerator[None]:
    await register_backend_tools(server)
    yield {}


def get_governed_mcp_server() -> FastMCP[Any]:
    """Singleton FastMCP server with tool discovery in lifespan."""
    global _mcp_instance
    if _mcp_instance is None:
        _mcp_instance = FastMCP(
            "Nexus MCP Proxy",
            instructions=(
                "Nexus governance proxy: tools are namespaced as {backend}__{tool}. "
                "Policies use mcp:{backend}:{tool} action IDs."
            ),
            lifespan=_nexus_mcp_lifespan,
        )
    return _mcp_instance


def get_streamable_http_app(path: str = "/mcp") -> StarletteWithLifespan:
    """ASGI app for Streamable HTTP (mount under FastAPI)."""
    global _http_app
    if _http_app is None:
        mcp = get_governed_mcp_server()
        _http_app = mcp.http_app(path=path, transport="streamable-http")
    return _http_app


async def run_stdio_async() -> None:
    """Run MCP proxy on stdio (for Claude Desktop, etc.)."""
    if settings.LOCAL_ONLY:
        logger.error("Cannot run MCP proxy in LOCAL_ONLY mode")
        raise RuntimeError("MCP proxy disabled when LOCAL_ONLY=true")
    mcp = get_governed_mcp_server()
    await mcp.run_stdio_async()
