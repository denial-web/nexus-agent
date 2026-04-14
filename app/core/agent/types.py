"""Tool execution types for the agentic loop."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ToolResult:
    """Outcome of a governed tool execution."""

    success: bool
    output: str
    error: str | None = None
    exit_code: int | None = None
    http_status: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class RegisteredTool:
    """Metadata + executor for a built-in or plugin tool."""

    name: str
    description: str
    parameters_schema: dict[str, Any]
    covernor_action: str

    def execute(self, args: dict[str, Any], workspace: Path) -> ToolResult:
        raise NotImplementedError
