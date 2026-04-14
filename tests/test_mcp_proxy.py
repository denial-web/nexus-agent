"""MCP governance proxy helpers and API."""

from __future__ import annotations

import pytest
from app.config import settings
from app.core.mcp.config import McpBackend, load_backends, save_backends
from app.core.mcp.proxy import GovernedMcpTool, exposed_tool_name, mcp_action_id
from app.models.policy import Policy
from sqlalchemy.orm import Session


@pytest.fixture(autouse=True)
def _isolated_mcp_backends_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "mcp_backends.json"
    p.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(settings, "MCP_BACKENDS_FILE", str(p))


def test_mcp_action_id_format() -> None:
    assert mcp_action_id("be", "tool") == "mcp:be:tool"


def test_exposed_tool_name_safe() -> None:
    assert ".." not in exposed_tool_name("b1", "t1")


def test_backends_roundtrip(tmp_path) -> None:
    p = str(tmp_path / "b.json")
    backends = [
        McpBackend(name="a", url="http://127.0.0.1:9/mcp", enabled=True),
    ]
    save_backends(p, backends)
    loaded = load_backends(p)
    assert len(loaded) == 1
    assert loaded[0].name == "a"


def test_api_mcp_backends_get(client) -> None:
    r = client.get("/api/mcp/backends")
    assert r.status_code == 200
    assert r.json() == []


def test_api_mcp_backends_post_local_only(client, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "LOCAL_ONLY", True)
    r = client.post(
        "/api/mcp/backends",
        json={"name": "x", "url": "http://127.0.0.1:1/mcp"},
    )
    assert r.status_code == 503


def test_governed_tool_local_only_raises(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    import mcp.types
    from app.config import settings
    from fastmcp.exceptions import ToolError

    monkeypatch.setattr(settings, "LOCAL_ONLY", True)
    monkeypatch.setattr(settings, "MCP_AUDIT_ALL", True)
    b = McpBackend(name="t", url="http://127.0.0.1:9/mcp")
    rt = mcp.types.Tool(name="echo", inputSchema={"type": "object", "properties": {}})
    tool = GovernedMcpTool(backend=b, remote_tool=rt, exposed_name="t__echo")

    with pytest.raises(ToolError):
        asyncio.run(tool.run({}))


def test_governor_deny_without_allow_policy(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    import mcp.types
    from app.config import settings
    from fastmcp.exceptions import ToolError

    monkeypatch.setattr(settings, "LOCAL_ONLY", False)
    monkeypatch.setattr(settings, "MCP_AUDIT_ALL", True)

    b = McpBackend(name="t", url="http://127.0.0.1:9/mcp")
    rt = mcp.types.Tool(name="echo", inputSchema={"type": "object", "properties": {}})
    tool = GovernedMcpTool(backend=b, remote_tool=rt, exposed_name="t__echo")

    with pytest.raises(ToolError):
        asyncio.run(tool.run({"x": 1}))


def test_governor_allow_forwards_monkeypatch(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp.types
    from app.config import settings
    from mcp.types import CallToolResult, TextContent

    monkeypatch.setattr(settings, "LOCAL_ONLY", False)
    monkeypatch.setattr(settings, "MCP_AUDIT_ALL", False)

    db_session.add(
        Policy(
            name="allow-mcp-test",
            action_pattern="mcp:t:echo",
            resource_pattern="*",
            decision="allow",
            risk_level="low",
            required_approvals="0",
            priority=5,
        )
    )
    db_session.commit()

    class _FakeResult:
        isError = False
        content = [TextContent(type="text", text="ok")]
        structuredContent = None
        meta = None

    class _FakeClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        async def __aenter__(self) -> object:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def call_tool(self, *_a: object, **_k: object) -> CallToolResult:
            return _FakeResult()  # type: ignore[return-value]

    monkeypatch.setattr("app.core.mcp.proxy.Client", _FakeClient)

    b = McpBackend(name="t", url="http://127.0.0.1:9/mcp")
    rt = mcp.types.Tool(name="echo", inputSchema={"type": "object", "properties": {}})
    tool = GovernedMcpTool(backend=b, remote_tool=rt, exposed_name="t__echo")
    import asyncio

    async def _run() -> object:
        return await tool.run({})

    res = asyncio.run(_run())
    assert res is not None


def test_tools_list_api_annotations(client) -> None:
    from app.core.mcp.config import save_backends

    save_backends(
        settings.MCP_BACKENDS_FILE,
        [McpBackend(name="stub", url="http://127.0.0.1:1/mcp", enabled=False)],
    )
    r = client.get("/api/mcp/backends/stub/tools")
    assert r.status_code in (404, 502, 503)
