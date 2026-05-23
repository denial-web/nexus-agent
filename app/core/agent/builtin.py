"""Built-in tools: shell, files, web, search."""

import ipaddress
import logging
import socket
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from app.config import settings
from app.core.agent.types import RegisteredTool, ToolResult

logger = logging.getLogger(__name__)


_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
    }
)


def _is_blocked_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified


def _hostname_is_blocked(host: str) -> bool:
    normalized = host.lower().strip(".")
    if not normalized:
        return True
    if normalized in _BLOCKED_HOSTNAMES or normalized.endswith(".internal"):
        return True

    literal = normalized.strip("[]")
    try:
        ipaddress.ip_address(literal)
    except ValueError:
        pass
    else:
        return _is_blocked_ip(literal)

    try:
        resolved = socket.getaddrinfo(normalized, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return True

    for _, _, _, _, sockaddr in resolved:
        addr = sockaddr[0]
        if _is_blocked_ip(addr):
            return True
    return False


def _is_blocked_host(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return True
        host = parsed.hostname or ""
        return _hostname_is_blocked(host)
    except Exception:
        return True


def _resolve_path(raw: str, workspace: Path) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (workspace / p).resolve()
    else:
        p = p.resolve()
    try:
        p.relative_to(workspace.resolve())
    except ValueError:
        raise ValueError(f"Path escapes workspace: {raw}") from None
    return p


class ShellExecTool(RegisteredTool):
    def __init__(self) -> None:
        super().__init__(
            name="shell_exec",
            description="Run a shell command in the agent workspace (cwd set to workspace).",
            parameters_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                },
                "required": ["command"],
            },
            covernor_action="shell_exec",
        )

    def execute(self, args: dict[str, Any], workspace: Path) -> ToolResult:
        cmd = str(args.get("command", "")).strip()
        if not cmd:
            return ToolResult(success=False, output="", error="Missing command", exit_code=-1)
        workspace.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=settings.AGENT_SHELL_TIMEOUT,
            )
            out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
            max_c = settings.AGENT_TOOL_OUTPUT_MAX_CHARS
            if len(out) > max_c:
                out = out[:max_c] + f"\n... [truncated, {len(out)} chars total]"
            ok = proc.returncode == 0
            return ToolResult(
                success=ok,
                output=out,
                error=None if ok else (proc.stderr or f"exit {proc.returncode}"),
                exit_code=proc.returncode,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="", error="Command timed out", exit_code=-1)
        except Exception as exc:
            logger.exception("shell_exec failed")
            return ToolResult(success=False, output="", error=str(exc), exit_code=-1)


class FileReadTool(RegisteredTool):
    def __init__(self) -> None:
        super().__init__(
            name="file_read",
            description="Read a text file under the workspace.",
            parameters_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative or absolute path under workspace"},
                },
                "required": ["path"],
            },
            covernor_action="file_read",
        )

    def execute(self, args: dict[str, Any], workspace: Path) -> ToolResult:
        raw = str(args.get("path", "")).strip()
        if not raw:
            return ToolResult(success=False, output="", error="Missing path", exit_code=-1)
        try:
            path = _resolve_path(raw, workspace)
            if not path.is_file():
                return ToolResult(success=False, output="", error=f"Not a file: {path}", exit_code=-1)
            text = path.read_text(encoding="utf-8", errors="replace")
            max_c = settings.AGENT_TOOL_OUTPUT_MAX_CHARS
            if len(text) > max_c:
                text = text[:max_c] + f"\n... [truncated, {len(text)} chars total]"
            return ToolResult(success=True, output=text, error=None, exit_code=0)
        except Exception as exc:
            return ToolResult(success=False, output="", error=str(exc), exit_code=-1)


class FileWriteTool(RegisteredTool):
    def __init__(self) -> None:
        super().__init__(
            name="file_write",
            description="Write text to a file under the workspace (creates parent dirs).",
            parameters_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            covernor_action="file_write",
        )

    def execute(self, args: dict[str, Any], workspace: Path) -> ToolResult:
        raw = str(args.get("path", "")).strip()
        content = str(args.get("content", ""))
        if not raw:
            return ToolResult(success=False, output="", error="Missing path", exit_code=-1)
        try:
            path = _resolve_path(raw, workspace)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return ToolResult(success=True, output=f"Wrote {len(content)} bytes to {path}", error=None, exit_code=0)
        except Exception as exc:
            return ToolResult(success=False, output="", error=str(exc), exit_code=-1)


class WebFetchTool(RegisteredTool):
    def __init__(self) -> None:
        super().__init__(
            name="web_fetch",
            description="HTTP GET request to a URL (response body text, truncated).",
            parameters_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                },
                "required": ["url"],
            },
            covernor_action="web_fetch",
        )

    def execute(self, args: dict[str, Any], workspace: Path) -> ToolResult:
        if settings.LOCAL_ONLY:
            return ToolResult(success=False, output="", error="web_fetch disabled in LOCAL_ONLY mode", exit_code=-1)
        url = str(args.get("url", "")).strip()
        if not url:
            return ToolResult(success=False, output="", error="Missing url", exit_code=-1)
        if _is_blocked_host(url):
            return ToolResult(success=False, output="", error=f"Blocked host: {url}", exit_code=-1)
        try:
            with httpx.Client(timeout=30.0, follow_redirects=False) as client:
                r = client.get(url)
                hops = 0
                while r.is_redirect and hops < 5:
                    location = r.headers.get("location", "")
                    next_url = urljoin(str(r.url), location)
                    if _is_blocked_host(next_url):
                        return ToolResult(
                            success=False,
                            output="",
                            error=f"Redirect to blocked host: {next_url}",
                            http_status=r.status_code,
                        )
                    r = client.get(next_url)
                    hops += 1
            body = r.text
            max_c = settings.AGENT_TOOL_OUTPUT_MAX_CHARS
            if len(body) > max_c:
                body = body[:max_c] + "\n... [truncated]"
            ok = 200 <= r.status_code < 300
            return ToolResult(
                success=ok,
                output=f"HTTP {r.status_code}\n\n{body}",
                error=None if ok else f"HTTP {r.status_code}",
                http_status=r.status_code,
            )
        except Exception as exc:
            return ToolResult(success=False, output="", error=str(exc), exit_code=-1)


class SearchTool(RegisteredTool):
    def __init__(self) -> None:
        super().__init__(
            name="search",
            description="Web search via Tavily (if TAVILY_API_KEY) or SerpAPI (if SERPAPI_API_KEY).",
            parameters_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
            covernor_action="search",
        )

    def execute(self, args: dict[str, Any], workspace: Path) -> ToolResult:
        if settings.LOCAL_ONLY:
            return ToolResult(success=False, output="", error="search disabled in LOCAL_ONLY mode", exit_code=-1)
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult(success=False, output="", error="Missing query", exit_code=-1)
        tavily = settings.TAVILY_API_KEY.strip()
        serp = settings.SERPAPI_API_KEY.strip()
        try:
            if tavily:
                with httpx.Client(timeout=30.0) as client:
                    r = client.post(
                        "https://api.tavily.com/search",
                        json={"api_key": tavily, "query": query, "max_results": 8},
                    )
                r.raise_for_status()
                data = r.json()
                lines = []
                for res in data.get("results", [])[:8]:
                    lines.append(f"- {res.get('title')}: {res.get('url')}\n  {res.get('content', '')[:500]}")
                text = "\n".join(lines) or str(data)
                return ToolResult(success=True, output=text, error=None, exit_code=0)
            if serp:
                with httpx.Client(timeout=30.0) as client:
                    r = client.get(
                        "https://serpapi.com/search.json",
                        params={"q": query, "api_key": serp, "engine": "google", "num": 8},
                    )
                r.raise_for_status()
                data = r.json()
                lines = []
                for item in data.get("organic_results", [])[:8]:
                    lines.append(f"- {item.get('title')}: {item.get('link')}\n  {item.get('snippet', '')}")
                text = "\n".join(lines) or str(data)
                return ToolResult(success=True, output=text, error=None, exit_code=0)
        except Exception as exc:
            logger.warning("Search API failed: %s", exc)
            return ToolResult(success=False, output="", error=str(exc), exit_code=-1)

        return ToolResult(
            success=False,
            output="",
            error="Configure TAVILY_API_KEY or SERPAPI_API_KEY for web search.",
            exit_code=-1,
        )


def default_builtin_tools() -> list[RegisteredTool]:
    return [
        ShellExecTool(),
        FileReadTool(),
        FileWriteTool(),
        WebFetchTool(),
        SearchTool(),
    ]
