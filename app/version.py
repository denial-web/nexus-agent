"""Nexus Agent version information.

Single source of truth for API version, project version, and build metadata.
The API version follows semver: bump MAJOR for breaking changes, MINOR for
backward-compatible additions, PATCH for fixes.
"""

from __future__ import annotations

import platform
import subprocess
from functools import lru_cache

API_VERSION = "1.0.0"
PROJECT_VERSION = "0.1.0"


@lru_cache(maxsize=1)
def get_git_sha() -> str | None:
    """Return the short git SHA of the current commit, or None."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def get_version_info() -> dict[str, str | None]:
    """Full version info for the /version endpoint."""
    return {
        "api_version": API_VERSION,
        "project_version": PROJECT_VERSION,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "git_sha": get_git_sha(),
    }
