"""Tests for the Covernor policy engine and token manager."""

import pytest
from app.core.covernor.policy_engine import evaluate_action
from app.core.covernor.token_manager import (
    get_public_key_pem,
    issue_token,
    reset_keys,
    verify_and_consume,
    verify_signature,
)


@pytest.fixture(autouse=True)
def _reset_token_keys():
    reset_keys()
    yield
    reset_keys()


class TestPolicyEngine:
    def test_default_deny_without_db(self):
        result = evaluate_action("unknown_action")
        assert result.decision == "deny"
        assert "No database session" in result.reason

    def test_default_deny_no_matching_policy(self, db_session):
        result = evaluate_action("totally_unknown_action", db_session=db_session)
        assert result.decision == "deny"
        assert "No matching policy" in result.reason


class TestTokenManager:
    def test_issue_and_verify(self):
        token = issue_token(trace_id="t1", action_type="respond", scope={"max_tokens": 100})
        assert token.token_id
        assert not token.used

        valid, reason = verify_and_consume(token.token_id)
        assert valid is True
        assert reason == "Valid"

    def test_single_use(self):
        token = issue_token(trace_id="t2", action_type="respond")

        valid, _ = verify_and_consume(token.token_id)
        assert valid is True

        valid, reason = verify_and_consume(token.token_id)
        assert valid is False
        assert "already consumed" in reason

    def test_unknown_token(self):
        valid, reason = verify_and_consume("nonexistent")
        assert valid is False
        assert "not found" in reason

    def test_expired_token(self):
        token = issue_token(trace_id="t3", action_type="respond", ttl_seconds=0)
        import time

        time.sleep(0.01)

        valid, reason = verify_and_consume(token.token_id)
        assert valid is False
        assert "expired" in reason

    def test_tampered_payload_rejected(self):
        from app.core.covernor import token_manager as tm

        token = issue_token(trace_id="t4", action_type="respond")
        tok = tm._issued_tokens[token.token_id]
        tok.trace_id = "tampered"

        valid, reason = verify_and_consume(token.token_id)
        assert valid is False
        assert "Invalid signature" in reason

    def test_public_key_pem(self):
        pem = get_public_key_pem()
        assert pem.startswith("-----BEGIN PUBLIC KEY-----")

    def test_verify_signature_round_trip(self):
        token = issue_token(trace_id="t5", action_type="file_write", scope={"path": "/tmp/x"})
        payload = {
            "token_id": token.token_id,
            "trace_id": token.trace_id,
            "action_type": token.action_type,
            "scope": token.scope,
            "issued_at": token.issued_at,
            "expires_at": token.expires_at,
        }
        assert verify_signature(payload, token.signature)

    def test_key_loaded_from_file(self, tmp_path):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        reset_keys()
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        p = tmp_path / "ecdsa.pem"
        p.write_bytes(pem)

        from unittest.mock import patch

        from app.config import settings

        with patch.object(settings, "ECDSA_PRIVATE_KEY_PATH", str(p)):
            reset_keys()
            t = issue_token(trace_id="fromfile", action_type="respond")
            ok, _ = verify_and_consume(t.token_id)
            assert ok is True
