"""
ECDSA capability token manager.

Issues single-use, scope-bound tokens after governance approval.
Each token cryptographically proves that a specific action was approved
through the K-of-N process.

Token metadata is persisted via ``capability_token_store`` (Redis when
``REDIS_URL`` is set, otherwise in-process per worker).
"""

import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.config import settings
from app.services.capability_token_store import (
    StoredCapabilityToken,
    get_token_store,
    reset_token_store,
)

logger = logging.getLogger(__name__)

_private_key: ec.EllipticCurvePrivateKey | None = None
_public_key: ec.EllipticCurvePublicKey | None = None
_key_lock = threading.Lock()


@dataclass
class CapabilityToken:
    token_id: str
    trace_id: str
    action_type: str
    scope: dict
    issued_at: str
    expires_at: str
    signature: str
    used: bool = False


def reset_keys() -> None:
    """Reset cached keys and issued tokens (for tests)."""
    global _private_key, _public_key
    _private_key = None
    _public_key = None
    reset_token_store()


def peek_token(token_id: str) -> CapabilityToken | None:
    """Return a token without consuming it (tests only)."""
    stored = get_token_store().peek(token_id)
    if stored is None:
        return None
    return CapabilityToken(**stored.__dict__)


def _ensure_keys() -> None:
    global _private_key, _public_key
    if _private_key is not None:
        return

    with _key_lock:
        if _private_key is not None:
            return

        path = (settings.ECDSA_PRIVATE_KEY_PATH or "").strip()
        if path and os.path.isfile(path):
            with open(path, "rb") as f:
                loaded = serialization.load_pem_private_key(f.read(), password=None)
                if not isinstance(loaded, ec.EllipticCurvePrivateKey):
                    raise TypeError("ECDSA_PRIVATE_KEY_PATH must contain an EC private key")
                _private_key = loaded
            logger.info("Loaded ECDSA private key from %s", path)
        else:
            if path:
                logger.warning("ECDSA_PRIVATE_KEY_PATH set but file missing; generating ephemeral key")
            else:
                logger.warning("ECDSA_PRIVATE_KEY_PATH unset; generating ephemeral in-memory key")
            _private_key = ec.generate_private_key(ec.SECP256R1())

        _public_key = _private_key.public_key()


def get_public_key_pem() -> str:
    """Return the PEM-encoded public key for offline verification."""
    _ensure_keys()
    assert _public_key is not None
    return _public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def _canonical_payload_bytes(payload: dict) -> bytes:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return raw.encode()


def _sign_payload(payload: dict) -> str:
    _ensure_keys()
    assert _private_key is not None
    data = _canonical_payload_bytes(payload)
    signature = _private_key.sign(data, ec.ECDSA(hashes.SHA256()))
    return signature.hex()


def verify_signature(payload: dict, signature_hex: str) -> bool:
    """Verify an ECDSA signature over the canonical JSON payload."""
    _ensure_keys()
    assert _public_key is not None
    try:
        sig = bytes.fromhex(signature_hex)
        _public_key.verify(sig, _canonical_payload_bytes(payload), ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError):
        return False


def issue_token(
    trace_id: str,
    action_type: str,
    scope: dict | None = None,
    ttl_seconds: int = 300,
) -> CapabilityToken:
    """Issue a single-use capability token for an approved action."""
    token_id = uuid.uuid4().hex
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=ttl_seconds)

    payload = {
        "token_id": token_id,
        "trace_id": trace_id,
        "action_type": action_type,
        "scope": scope or {},
        "issued_at": now.isoformat(),
        "expires_at": expires.isoformat(),
    }

    signature = _sign_payload(payload)

    token = CapabilityToken(
        token_id=token_id,
        trace_id=trace_id,
        action_type=action_type,
        scope=scope or {},
        issued_at=now.isoformat(),
        expires_at=expires.isoformat(),
        signature=signature,
    )

    stored = StoredCapabilityToken(**token.__dict__)
    get_token_store().put(stored, ttl_seconds=max(ttl_seconds, 1))
    logger.info("Issued capability token %s for trace %s action %s", token_id, trace_id, action_type)
    return token


def verify_and_consume(token_id: str) -> tuple[bool, str]:
    """
    Verify and consume a capability token (single-use).

    Returns (valid, reason).
    """
    stored = get_token_store().pop(token_id)
    if stored is None:
        return False, "Token not found"

    expires = datetime.fromisoformat(stored.expires_at)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if datetime.now(UTC) > expires:
        return False, "Token expired"

    payload = {
        "token_id": stored.token_id,
        "trace_id": stored.trace_id,
        "action_type": stored.action_type,
        "scope": stored.scope,
        "issued_at": stored.issued_at,
        "expires_at": stored.expires_at,
    }
    if not verify_signature(payload, stored.signature):
        return False, "Invalid signature"

    logger.info("Consumed token %s", token_id)
    return True, "Valid"
