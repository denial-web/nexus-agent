"""Heuristic conversion of Markdown instructions into Nexus skill steps."""

from __future__ import annotations

import re
from typing import Any


def markdown_to_steps(markdown_body: str) -> list[dict[str, Any]]:
    """Convert SKILL.md body into tool_call / final_answer / instruction steps."""
    steps: list[dict[str, Any]] = []
    lines = markdown_body.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        # Fenced shell blocks
        if line.startswith("```"):
            block: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            if i < len(lines) and lines[i].strip().startswith("```"):
                i += 1
            inner = "\n".join(block).strip()
            low = inner.lower()
            if low.startswith("curl ") or "curl " in low[:80]:
                steps.append(
                    {
                        "action": "tool_call",
                        "tool": "web_fetch",
                        "arguments_template": {"url": _extract_url(inner) or "https://example.invalid"},
                        "expect_success": True,
                    }
                )
            else:
                steps.append(
                    {
                        "action": "tool_call",
                        "tool": "shell_exec",
                        "arguments_template": {"command": inner[:4000]},
                        "expect_success": True,
                    }
                )
            continue
        # Inline backticks (short shell)
        if "`" in line:
            cmds = re.findall(r"`([^`]+)`", line)
            for c in cmds:
                cl = c.strip()
                if len(cl) > 2 and not cl.startswith("http"):
                    steps.append(
                        {
                            "action": "tool_call",
                            "tool": "shell_exec",
                            "arguments_template": {"command": cl[:4000]},
                            "expect_success": True,
                        }
                    )
        # File read hints
        lowl = line.lower()
        if any(x in lowl for x in ("read file", "open file", "read the file")):
            m = re.search(r"['\"]?([\w./\\-]+\.(?:txt|md|json|yaml|yml|py))['\"]?", line, re.I)
            path = m.group(1) if m else "README.md"
            steps.append(
                {
                    "action": "tool_call",
                    "tool": "file_read",
                    "arguments_template": {"path": path},
                    "expect_success": True,
                }
            )
        elif any(x in lowl for x in ("write", "create a file", "save to")):
            m = re.search(r"['\"]?([\w./\\-]+\.\w+)['\"]?", line)
            path = m.group(1) if m else "output.txt"
            steps.append(
                {
                    "action": "tool_call",
                    "tool": "file_write",
                    "arguments_template": {"path": path, "content": "(see instructions above)"},
                    "expect_success": True,
                }
            )
        elif _looks_like_instruction_only(line):
            steps.append({"action": "instruction", "content": line[:2000]})
        i += 1

    if not steps:
        steps.append(
            {
                "action": "instruction",
                "content": (markdown_body[:4000] if markdown_body else "Imported skill"),
            }
        )
    return steps


def _extract_url(text: str) -> str | None:
    m = re.search(r"https?://[^\s\"'<>]+", text)
    return m.group(0) if m else None


def _looks_like_instruction_only(line: str) -> bool:
    if line.startswith(("#", "-", "*", "1.", "2.", "3.")):
        return True
    return len(line) > 40
