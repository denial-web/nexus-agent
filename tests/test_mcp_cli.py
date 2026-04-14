"""CLI wiring for MCP and skills import."""

from __future__ import annotations

import sys

import pytest


def test_mcp_subparser_help_exits_zero() -> None:
    import app.cli

    with pytest.raises(SystemExit) as exc:
        sys.argv = ["nexus", "mcp", "add", "--help"]
        app.cli.main()
    assert exc.value.code == 0


def test_skills_import_subparser_help_exits_zero() -> None:
    import app.cli

    with pytest.raises(SystemExit) as exc:
        sys.argv = ["nexus", "skills", "import", "--help"]
        app.cli.main()
    assert exc.value.code == 0


def test_skills_requires_subcommand() -> None:
    import app.cli

    with pytest.raises(SystemExit):
        sys.argv = ["nexus", "skills"]
        app.cli.main()
