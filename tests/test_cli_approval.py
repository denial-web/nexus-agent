"""CLI approval command behavior."""

from __future__ import annotations

from argparse import Namespace
from typing import Any


def test_cli_approve_requires_explicit_reviewer(monkeypatch, capsys) -> None:
    import app.cli

    def fail_post(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("approve should not POST without a reviewer id")

    monkeypatch.delenv("NEXUS_APPROVER_ID", raising=False)
    monkeypatch.setattr(app.cli.httpx, "post", fail_post)

    rc = app.cli.cmd_approve(Namespace(request_id="req-1", approver=None))

    assert rc == 2
    err = capsys.readouterr().err
    assert "Approver reviewer ID required" in err
    assert "APPROVAL_REVIEWERS" in err


def test_cli_approve_uses_env_reviewer_and_api_key(monkeypatch, capsys) -> None:
    import app.cli

    captured: dict[str, Any] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"status": "approved"}

    def fake_post(url: str, json: dict[str, Any], headers: dict[str, str], timeout: float) -> FakeResponse:
        captured.update({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setenv("NEXUS_APPROVER_ID", "alice")
    monkeypatch.setenv("NEXUS_API_KEY", "beta-key")
    monkeypatch.setattr(app.cli.httpx, "post", fake_post)

    rc = app.cli.cmd_approve(Namespace(request_id="req-1", approver=None))

    assert rc == 0
    assert captured["json"] == {"approver_id": "alice", "decision": "approve"}
    assert captured["headers"]["X-API-Key"] == "beta-key"
    assert '"status": "approved"' in capsys.readouterr().out
