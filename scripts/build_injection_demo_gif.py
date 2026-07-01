#!/usr/bin/env python3
"""Build paced asciinema cast + GIF for README injection demo."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ASSETS = REPO / "docs" / "assets"
CAST_PATH = ASSETS / "injection_demo.cast"
GIF_PATH = ASSETS / "injection_demo.gif"
PAUSE_S = 0.6


def _run_demo() -> str:
    proc = subprocess.run(
        [sys.executable, str(REPO / "examples" / "injection_demo.py"), "--immune-only"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def _chunks(text: str) -> list[str]:
    lines = text.splitlines(keepends=True)
    chunks: list[str] = []
    i = 0
    while i < len(lines) and not lines[i].startswith("["):
        i += 1
    chunks.append("".join(lines[:i]))

    while i < len(lines):
        if re.match(r"^\[(PASS|BLOCK|FLAG|ERR )", lines[i]):
            block = [lines[i]]
            i += 1
            while i < len(lines) and not re.match(r"^\[(PASS|BLOCK|FLAG|ERR )", lines[i]):
                block.append(lines[i])
                i += 1
            chunks.append("".join(block))
        else:
            if i < len(lines):
                chunks.append("".join(lines[i:]))
            break
    return [c for c in chunks if c]


def _write_cast(chunks: list[str]) -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    header = {
        "version": 3,
        "term": {"cols": 100, "rows": 48},
        "timestamp": 1,
        "command": "python examples/injection_demo.py --immune-only",
        "title": "Nexus injection demo",
        "env": {"SHELL": "/bin/zsh"},
    }
    events: list[str] = []
    t = 0.3
    acc = ""
    for chunk in chunks:
        acc += chunk
        events.append(f"[{t:.3f}, \"o\", {json.dumps(acc)}]")
        t += PAUSE_S
    events.append(f"[{t:.3f}, \"x\", \"0\"]")
    CAST_PATH.write_text(json.dumps(header) + "\n" + "\n".join(events) + "\n", encoding="utf-8")


def _render_gif() -> None:
    if not shutil.which("agg"):
        print("agg not found — install with: brew install agg", file=sys.stderr)
        sys.exit(1)
    subprocess.run(
        ["agg", "--font-size", "14", "--theme", "monokai", str(CAST_PATH), str(GIF_PATH)],
        check=True,
    )


def main() -> None:
    text = _run_demo()
    _write_cast(_chunks(text))
    _render_gif()
    print(f"Wrote {CAST_PATH}")
    print(f"Wrote {GIF_PATH} ({GIF_PATH.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
