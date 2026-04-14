"""Tool registry — resolve tools by name."""

from pathlib import Path
from typing import Any

from app.core.agent.builtin import default_builtin_tools
from app.core.agent.types import RegisteredTool, ToolResult


class ToolRegistry:
    def __init__(self, extra: list[RegisteredTool] | None = None) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        for t in default_builtin_tools():
            self._tools[t.name] = t
        if extra:
            for t in extra:
                self._tools[t.name] = t

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[RegisteredTool]:
        return list(self._tools.values())

    def tool_definitions_json(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for t in self._tools.values():
            out.append(
                {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters_schema,
                }
            )
        return out

    def execute(self, name: str, args: dict[str, Any], workspace: Path) -> ToolResult:
        tool = self.get(name)
        if not tool:
            return ToolResult(success=False, output="", error=f"Unknown tool: {name}", exit_code=-1)
        return tool.execute(args, workspace)
