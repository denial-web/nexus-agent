"""
`nexus memory` CLI tests (Phase 12B Week 4).

Covers:
- help-exits-zero for every subcommand (subparser wiring)
- happy-path JSON output for each subcommand (stubs `httpx` at module scope)
- error surfacing (503 memory_disabled, 403 governance_denied)
- pretty-print output for the showcase `verify` command
- exit codes: 0 ok, 1 transport/auth error, 2 hash-chain broken

The tests stub `httpx.get` / `httpx.post` directly because the CLI imports
httpx at module top; a full TestClient roundtrip would couple these tests
to FastAPI lifespan ordering, which is more than the CLI contract requires.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any

import app.cli as cli
import httpx
import pytest

# ────────────────────────────────────────────────────────────────────────
# Shared test scaffolding
# ────────────────────────────────────────────────────────────────────────


@dataclass
class FakeResponse:
    """Minimal httpx.Response stand-in with the surface the CLI uses.

    Real `httpx.Response` requires a `Request` object to construct, so
    this is easier than coercing one; we only need `.raise_for_status()`,
    `.json()`, `.text`, and `.status_code`.
    """

    status_code: int
    payload: Any = None
    text: str = ""

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            req = httpx.Request("GET", "http://test/")
            resp = httpx.Response(
                self.status_code,
                json=self.payload if isinstance(self.payload, dict | list) else None,
                text=self.text if not isinstance(self.payload, dict | list) else None,
                request=req,
            )
            raise httpx.HTTPStatusError(f"{self.status_code}", request=req, response=resp)

    def json(self) -> Any:
        return self.payload


@pytest.fixture
def stub_httpx(monkeypatch):
    """Replace `httpx.get`/`httpx.post` with callable-capturing stubs.

    Returns a `calls` dict the tests can append to per-request scripts.
    """
    calls: dict[str, list[dict[str, Any]]] = {"get": [], "post": []}
    script: dict[str, list[FakeResponse]] = {"get": [], "post": []}

    def _get(url, headers=None, params=None, timeout=None, **kw):
        calls["get"].append({"url": url, "params": params})
        if not script["get"]:
            raise AssertionError(f"Unexpected GET {url}; no stub queued")
        return script["get"].pop(0)

    def _post(url, headers=None, json=None, timeout=None, **kw):
        calls["post"].append({"url": url, "json": json})
        if not script["post"]:
            raise AssertionError(f"Unexpected POST {url}; no stub queued")
        return script["post"].pop(0)

    monkeypatch.setattr(cli.httpx, "get", _get)
    monkeypatch.setattr(cli.httpx, "post", _post)
    return {"calls": calls, "script": script}


def _run(argv: list[str]) -> int:
    """Run the CLI with given argv; return the exit code."""
    sys.argv = ["nexus", *argv]
    try:
        cli.main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


# ────────────────────────────────────────────────────────────────────────
# Subparser wiring — help exits zero for every subcommand
# ────────────────────────────────────────────────────────────────────────


class TestMemorySubparsers:
    @pytest.mark.parametrize(
        "sub",
        ["recall", "history", "explain", "forget", "stats", "verify"],
    )
    def test_help_exits_zero(self, sub):
        rc = _run(["memory", sub, "--help"])
        assert rc == 0

    def test_memory_requires_subcommand(self):
        rc = _run(["memory"])
        assert rc != 0

    def test_top_level_help_lists_memory(self, capsys):
        rc = _run(["--help"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "memory" in out


# ────────────────────────────────────────────────────────────────────────
# recall
# ────────────────────────────────────────────────────────────────────────


class TestMemoryRecall:
    def test_json_output(self, stub_httpx, capsys):
        stub_httpx["script"]["get"].append(
            FakeResponse(
                200,
                {
                    "total": 2,
                    "limit": 50,
                    "offset": 0,
                    "beliefs": [
                        {
                            "id": "b-aaaaaaaaaaaa",
                            "entity": "user:alice",
                            "predicate": "prefers",
                            "value": "dark_mode",
                            "mean": 0.9,
                            "is_current": True,
                            "user_id": "alice",
                        }
                    ],
                },
            )
        )
        rc = _run(["memory", "recall", "--user", "alice", "--limit", "10", "--json"])
        out = capsys.readouterr().out
        assert rc == 0
        parsed = json.loads(out)
        assert parsed["total"] == 2
        assert stub_httpx["calls"]["get"][0]["params"] == {
            "limit": 10,
            "user_id": "alice",
        }

    def test_pretty_output(self, stub_httpx, capsys):
        stub_httpx["script"]["get"].append(
            FakeResponse(
                200,
                {
                    "total": 1,
                    "limit": 50,
                    "offset": 0,
                    "beliefs": [
                        {
                            "id": "b-bbbbbbbbbbbb1234",
                            "entity": "user:bob",
                            "predicate": "prefers",
                            "value": "light_mode",
                            "mean": 0.85,
                            "is_current": True,
                            "user_id": "bob",
                        }
                    ],
                },
            )
        )
        rc = _run(["memory", "recall"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "user:bob" in out
        assert "light_mode" in out
        assert "p=0.85" in out
        assert "[live]" in out

    def test_empty_result(self, stub_httpx, capsys):
        stub_httpx["script"]["get"].append(FakeResponse(200, {"total": 0, "limit": 50, "offset": 0, "beliefs": []}))
        rc = _run(["memory", "recall"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "No beliefs matched" in out

    def test_503_memory_disabled(self, stub_httpx, capsys):
        stub_httpx["script"]["get"].append(
            FakeResponse(
                503,
                {
                    "error": {
                        "code": "memory_disabled",
                        "message": "Belief memory is disabled.",
                    }
                },
            )
        )
        rc = _run(["memory", "recall"])
        err = capsys.readouterr().err
        assert rc == 1
        assert "memory_disabled" in err


# ────────────────────────────────────────────────────────────────────────
# history / explain / forget / stats
# ────────────────────────────────────────────────────────────────────────


class TestMemoryHistory:
    def test_pretty_output(self, stub_httpx, capsys):
        stub_httpx["script"]["get"].append(
            FakeResponse(
                200,
                {
                    "entity": "user:alice",
                    "predicate": "prefers",
                    "versions": [
                        {
                            "id": "v-oldaaaaaaaa",
                            "value": "blue",
                            "mean": 0.6,
                            "observed_at": "2026-04-01T00:00:00+00:00",
                            "superseded_at": "2026-04-15T00:00:00+00:00",
                        },
                        {
                            "id": "v-newbbbbbbbb",
                            "value": "green",
                            "mean": 0.9,
                            "observed_at": "2026-04-15T00:00:00+00:00",
                            "superseded_at": None,
                        },
                    ],
                },
            )
        )
        rc = _run(["memory", "history", "v-newbbbbbbbb"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "user:alice · prefers" in out
        assert "LIVE" in out
        assert "superseded 2026-04-15" in out


class TestMemoryExplain:
    def test_pretty_output(self, stub_httpx, capsys):
        stub_httpx["script"]["get"].append(
            FakeResponse(
                200,
                {
                    "belief": {
                        "id": "b-explainaaaaa",
                        "entity": "user:alice",
                        "predicate": "prefers",
                    },
                    "query_text": "theme",
                    "rrf_score": 0.1234,
                    "rank_in_scope": 1,
                    "signals": [
                        {"signal": "confidence", "score": 0.9},
                        {"signal": "keyword_overlap", "score": 0.5},
                    ],
                },
            )
        )
        rc = _run(["memory", "explain", "b-explainaaaaa", "--query", "theme"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "rank_in_scope: 1" in out
        assert "confidence" in out
        assert "keyword_overlap" in out
        # Query param was forwarded.
        assert stub_httpx["calls"]["get"][0]["params"] == {"query_text": "theme"}


class TestMemoryForget:
    def test_sends_user_and_predicate(self, stub_httpx, capsys):
        stub_httpx["script"]["post"].append(
            FakeResponse(
                200,
                {
                    "tombstoned": 3,
                    "entity": "user:alice",
                    "predicate": "prefers",
                    "user_id": "alice",
                },
            )
        )
        rc = _run(
            [
                "memory",
                "forget",
                "user:alice",
                "--predicate",
                "prefers",
                "--user",
                "alice",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "Tombstoned 3 belief(s)" in out
        assert stub_httpx["calls"]["post"][0]["json"] == {
            "entity": "user:alice",
            "predicate": "prefers",
            "user_id": "alice",
        }


class TestMemoryStats:
    def test_pretty_output(self, stub_httpx, capsys):
        stub_httpx["script"]["get"].append(
            FakeResponse(
                200,
                {
                    "enabled": True,
                    "total_live": 7,
                    "total_tombstoned": 2,
                    "by_entity_type": {"preference": 5, "identity": 2},
                    "by_source_type": {"user_stated": 7},
                    "decay_profile": {"preference": "180d", "identity": "inf"},
                },
            )
        )
        rc = _run(["memory", "stats"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "total_live       : 7" in out
        assert "preference" in out
        assert "180d" in out


# ────────────────────────────────────────────────────────────────────────
# verify — the showcase command
# ────────────────────────────────────────────────────────────────────────


class TestMemoryVerify:
    def test_verified_ok(self, stub_httpx, capsys):
        stub_httpx["script"]["get"].append(
            FakeResponse(
                200,
                {
                    "verified": True,
                    "rows_checked": 42,
                    "first_break_at": None,
                    "reason": None,
                    "scope_user_ids": ["alice", "bob", None],
                    "checked_user_count": 3,
                    "as_of": None,
                },
            )
        )
        rc = _run(["memory", "verify"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "VERIFIED" in out
        assert "Rows checked : 42" in out
        assert "Chains walked: 3" in out

    def test_broken_chain_exits_2(self, stub_httpx, capsys):
        """Broken chain is a FINDING, not a transport error. Exit 2 lets
        CI gates distinguish 'the integrity service is unreachable'
        (rc=1) from 'the chain is tampered' (rc=2)."""
        stub_httpx["script"]["get"].append(
            FakeResponse(
                200,
                {
                    "verified": False,
                    "rows_checked": 12,
                    "first_break_at": "b-tampered-id",
                    "reason": "belief_hash mismatch at belief_id=b-tampered-id",
                    "scope_user_ids": ["victim"],
                    "checked_user_count": 1,
                    "as_of": None,
                },
            )
        )
        rc = _run(["memory", "verify", "--user", "victim"])
        out = capsys.readouterr().out
        assert rc == 2
        assert "BROKEN" in out
        assert "b-tampered-id" in out
        assert "mismatch" in out

    def test_json_output(self, stub_httpx, capsys):
        stub_httpx["script"]["get"].append(
            FakeResponse(
                200,
                {
                    "verified": True,
                    "rows_checked": 1,
                    "first_break_at": None,
                    "reason": None,
                    "scope_user_ids": ["alice"],
                    "checked_user_count": 1,
                    "as_of": "2026-04-17T00:00:00+00:00",
                },
            )
        )
        rc = _run(
            [
                "memory",
                "verify",
                "--user",
                "alice",
                "--as-of",
                "2026-04-17T00:00:00+00:00",
                "--json",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0
        parsed = json.loads(out)
        assert parsed["verified"] is True
        assert stub_httpx["calls"]["get"][0]["params"] == {
            "user_id": "alice",
            "as_of": "2026-04-17T00:00:00+00:00",
        }

    def test_scope_null_forwards_scope_all_false(self, stub_httpx, capsys):
        stub_httpx["script"]["get"].append(
            FakeResponse(
                200,
                {
                    "verified": True,
                    "rows_checked": 0,
                    "first_break_at": None,
                    "reason": None,
                    "scope_user_ids": [None],
                    "checked_user_count": 1,
                    "as_of": None,
                },
            )
        )
        rc = _run(["memory", "verify", "--scope-null"])
        assert rc == 0
        assert stub_httpx["calls"]["get"][0]["params"] == {"scope_all": "false"}

    def test_403_governance_denied(self, stub_httpx, capsys):
        stub_httpx["script"]["get"].append(
            FakeResponse(
                403,
                {
                    "error": {
                        "code": "governance_denied",
                        "message": "Memory integrity verification denied by policy",
                    }
                },
            )
        )
        rc = _run(["memory", "verify"])
        err = capsys.readouterr().err
        assert rc == 1
        assert "governance_denied" in err

    def test_transport_error_exits_1(self, monkeypatch, capsys):
        """Connection failures are rc=1, distinct from rc=2 for tamper."""

        def _boom(*a, **kw):
            raise httpx.ConnectError("no route to host")

        monkeypatch.setattr(cli.httpx, "get", _boom)
        rc = _run(["memory", "verify"])
        err = capsys.readouterr().err
        assert rc == 1
        assert "Request failed" in err
