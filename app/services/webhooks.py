"""
Webhook dispatcher — fires HMAC-signed HTTP POST notifications for system events.

Events:
  - approval_needed: K-of-N approval required for a pipeline response
  - critic_halt: Critic tree halted a generation
  - circuit_open: LLM provider circuit breaker tripped to OPEN
  - input_blocked: Immune scanner blocked an input prompt
  - export_complete: Training data export finished
  - output_blocked: Output scan blocked a leaked secret

Design:
  - Async dispatch via thread pool (non-blocking to the caller)
  - HMAC-SHA256 signing when webhook has a secret configured
  - Configurable retry with exponential backoff
  - Auto-disable after N consecutive failures
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()

_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 1.0
_REQUEST_TIMEOUT = 10.0
_MAX_CONSECUTIVE_FAILURES = 10


class WebhookEvent(StrEnum):
    APPROVAL_NEEDED = "approval_needed"
    CRITIC_HALT = "critic_halt"
    CIRCUIT_OPEN = "circuit_open"
    INPUT_BLOCKED = "input_blocked"
    OUTPUT_BLOCKED = "output_blocked"
    EXPORT_COMPLETE = "export_complete"


@dataclass
class WebhookPayload:
    event: str
    timestamp: str
    data: dict[str, Any] = field(default_factory=dict)


def _get_pool() -> ThreadPoolExecutor:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(
                max_workers=settings.WEBHOOK_WORKERS,
                thread_name_prefix="webhook",
            )
        return _pool


def shutdown_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.shutdown(wait=False)
        _pool = None


def _sign_payload(payload_bytes: bytes, secret: str) -> str:
    return hmac.new(
        secret.encode(), payload_bytes, hashlib.sha256,
    ).hexdigest()


def _deliver(
    url: str,
    payload: WebhookPayload,
    secret: str | None,
    webhook_id: str,
) -> bool:
    """Deliver a webhook with retries. Returns True on success."""
    import urllib.error
    import urllib.request

    body = json.dumps(
        {"event": payload.event, "timestamp": payload.timestamp, "data": payload.data},
        default=str,
    ).encode()

    headers = {"Content-Type": "application/json", "User-Agent": "Nexus-Agent-Webhook/1.0"}
    if secret:
        sig = _sign_payload(body, secret)
        headers["X-Nexus-Signature-256"] = f"sha256={sig}"

    for attempt in range(_MAX_RETRIES):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                status = resp.status
                if 200 <= status < 300:
                    logger.info(
                        "Webhook %s delivered: event=%s url=%s status=%d",
                        webhook_id, payload.event, url, status,
                    )
                    return True
                logger.warning(
                    "Webhook %s non-2xx: event=%s url=%s status=%d (attempt %d/%d)",
                    webhook_id, payload.event, url, status, attempt + 1, _MAX_RETRIES,
                )
        except Exception as exc:
            logger.warning(
                "Webhook %s failed: event=%s url=%s error=%s (attempt %d/%d)",
                webhook_id, payload.event, url, exc, attempt + 1, _MAX_RETRIES,
            )
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_BACKOFF_BASE * (2 ** attempt))
                continue
            _record_failure(webhook_id, str(exc))
            return False

    _record_failure(webhook_id, "Max retries exceeded")
    return False


def _record_failure(webhook_id: str, error: str) -> None:
    """Increment failure count; auto-disable after too many consecutive failures."""
    try:
        from app.db import SessionLocal
        from app.models.webhook import Webhook

        session = SessionLocal()
        try:
            wh = session.query(Webhook).filter_by(id=webhook_id).first()
            if wh:
                wh.failure_count = (wh.failure_count or 0) + 1
                wh.last_error = error
                if wh.failure_count >= _MAX_CONSECUTIVE_FAILURES:
                    wh.enabled = False
                    logger.warning(
                        "Webhook %s auto-disabled after %d consecutive failures",
                        webhook_id, wh.failure_count,
                    )
                session.commit()
        finally:
            session.close()
    except Exception:
        logger.exception("Failed to record webhook failure for %s", webhook_id)


def _record_success(webhook_id: str) -> None:
    """Reset failure count on successful delivery."""
    try:
        from app.db import SessionLocal
        from app.models.webhook import Webhook

        session = SessionLocal()
        try:
            wh = session.query(Webhook).filter_by(id=webhook_id).first()
            if wh:
                wh.failure_count = 0
                wh.last_error = None
                wh.last_triggered_at = datetime.now(UTC)
                session.commit()
        finally:
            session.close()
    except Exception:
        logger.exception("Failed to record webhook success for %s", webhook_id)


def _dispatch_one(
    url: str,
    payload: WebhookPayload,
    secret: str | None,
    webhook_id: str,
) -> None:
    success = _deliver(url, payload, secret, webhook_id)
    if success:
        _record_success(webhook_id)


def fire_event(
    event: WebhookEvent | str,
    data: dict[str, Any] | None = None,
    db_session: Any | None = None,
) -> int:
    """
    Fire a webhook event to all subscribed endpoints.

    Returns the number of webhooks queued for delivery.
    Pass db_session for testing; otherwise uses SessionLocal.
    """
    if not settings.WEBHOOKS_ENABLED:
        return 0

    event_str = event.value if isinstance(event, WebhookEvent) else event
    payload = WebhookPayload(
        event=event_str,
        timestamp=datetime.now(UTC).isoformat(),
        data=data or {},
    )

    try:
        from app.models.webhook import Webhook

        own_session = False
        session = db_session
        if session is None:
            from app.db import SessionLocal

            session = SessionLocal()
            own_session = True
        try:
            webhooks = session.query(Webhook).filter_by(enabled=True).all()
            matched = [
                wh for wh in webhooks
                if event_str in (wh.events or [])
                or "*" in (wh.events or [])
            ]
        finally:
            if own_session:
                session.close()
    except Exception:
        logger.exception("Failed to query webhooks for event %s", event_str)
        return 0

    if not matched:
        return 0

    pool = _get_pool()
    count = 0
    for wh in matched:
        try:
            pool.submit(_dispatch_one, wh.url, payload, wh.secret, wh.id)
            count += 1
        except Exception:
            logger.exception("Failed to submit webhook %s to pool", wh.id)

    return count


def verify_signature(payload_bytes: bytes, secret: str, signature_header: str) -> bool:
    """Verify an incoming webhook signature (for test endpoint or receivers)."""
    if not signature_header.startswith("sha256="):
        return False
    expected = _sign_payload(payload_bytes, secret)
    return hmac.compare_digest(f"sha256={expected}", signature_header)
