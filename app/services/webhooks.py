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
import random
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

_RETRYABLE_STATUS_CODES = frozenset(range(500, 600)) | {408, 429}


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


def shutdown_pool(wait: bool = False, timeout: float | None = None) -> None:
    """Shut down the dispatcher thread pool.

    Pass wait=True to block until in-flight deliveries complete — useful in
    tests so background threads don't outlive pytest's stdout/stderr capture
    and emit spurious "I/O operation on closed file" tracebacks.
    """
    global _pool
    if _pool is None:
        return
    pool = _pool
    _pool = None
    try:
        if wait and timeout is not None:
            try:
                pool.shutdown(wait=True, cancel_futures=True)
            except TypeError:
                pool.shutdown(wait=True)
        else:
            pool.shutdown(wait=wait)
    except Exception:
        pass


def _safe_log_exception(msg: str, *args: Any) -> None:
    """Log an exception, swallowing errors if stdout/stderr is already closed.

    Webhook dispatch runs on a background thread pool; during process shutdown
    (or pytest teardown) the logger's stream may be closed before the worker
    finishes, which would otherwise surface as noisy unhandled tracebacks.
    """
    try:
        logger.exception(msg, *args)
    except (ValueError, OSError):
        pass


def _sign_payload(payload_bytes: bytes, secret: str) -> str:
    return hmac.new(
        secret.encode(), payload_bytes, hashlib.sha256,
    ).hexdigest()


def compute_backoff(
    attempt: int,
    base: float | None = None,
    maximum: float | None = None,
) -> float:
    """Compute exponential backoff delay with full jitter.

    delay = random(0, min(max_backoff, base * 2^attempt))

    Full jitter avoids thundering-herd effects when many webhooks
    retry simultaneously after a downstream outage.
    """
    if base is None:
        base = settings.WEBHOOK_BACKOFF_BASE
    if maximum is None:
        maximum = settings.WEBHOOK_BACKOFF_MAX
    exp_delay = min(maximum, base * (2 ** attempt))
    return random.uniform(0, exp_delay)


def _is_retryable_status(status: int) -> bool:
    return status in _RETRYABLE_STATUS_CODES


def _deliver(
    url: str,
    payload: WebhookPayload,
    secret: str | None,
    webhook_id: str,
) -> bool:
    """Deliver a webhook with exponential backoff + jitter retries.

    Retries on connection errors and retryable HTTP status codes
    (5xx, 408, 429). Non-retryable 4xx responses fail immediately.
    """
    import urllib.error
    import urllib.request

    max_retries = settings.WEBHOOK_MAX_RETRIES
    timeout = settings.WEBHOOK_REQUEST_TIMEOUT

    body = json.dumps(
        {"event": payload.event, "timestamp": payload.timestamp, "data": payload.data},
        default=str,
    ).encode()

    headers = {"Content-Type": "application/json", "User-Agent": "Nexus-Agent-Webhook/1.0"}
    if secret:
        sig = _sign_payload(body, secret)
        headers["X-Nexus-Signature-256"] = f"sha256={sig}"

    last_error = ""
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                if 200 <= status < 300:
                    logger.info(
                        "Webhook %s delivered: event=%s url=%s status=%d",
                        webhook_id, payload.event, url, status,
                    )
                    return True

                last_error = f"HTTP {status}"
                if not _is_retryable_status(status):
                    logger.warning(
                        "Webhook %s non-retryable status: event=%s url=%s status=%d",
                        webhook_id, payload.event, url, status,
                    )
                    _record_failure(webhook_id, last_error)
                    return False

                logger.warning(
                    "Webhook %s retryable status: event=%s url=%s status=%d (attempt %d/%d)",
                    webhook_id, payload.event, url, status, attempt + 1, max_retries,
                )
        except Exception as exc:
            last_error = str(exc)
            logger.warning(
                "Webhook %s failed: event=%s url=%s error=%s (attempt %d/%d)",
                webhook_id, payload.event, url, exc, attempt + 1, max_retries,
            )

        if attempt < max_retries - 1:
            delay = compute_backoff(attempt)
            time.sleep(delay)

    _record_failure(webhook_id, last_error or "Max retries exceeded")
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
                max_failures = settings.WEBHOOK_MAX_CONSECUTIVE_FAILURES
                if wh.failure_count >= max_failures:
                    wh.enabled = False
                    logger.warning(
                        "Webhook %s auto-disabled after %d consecutive failures",
                        webhook_id, wh.failure_count,
                    )
                session.commit()
        finally:
            session.close()
    except Exception:
        _safe_log_exception("Failed to record webhook failure for %s", webhook_id)


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
        _safe_log_exception("Failed to record webhook success for %s", webhook_id)


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
