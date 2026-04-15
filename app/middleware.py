"""Security middleware: API key authentication and rate limiting."""

import asyncio
import hashlib
import hmac
import logging
from typing import Any

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from app.config import settings

logger = logging.getLogger(__name__)

_EXEMPT_PATHS = {"/health", "/health/ready", "/docs", "/redoc", "/openapi.json"}


def _safe_key_compare(provided: str, expected: str) -> bool:
    """Timing-safe comparison that hashes both sides to fixed length."""
    a = hashlib.sha256(provided.encode()).digest()
    b = hashlib.sha256(expected.encode()).digest()
    return hmac.compare_digest(a, b)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add baseline security headers to every response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response


_rate_limiter_instance: "RateLimitMiddleware | None" = None


class AuthMiddleware(BaseHTTPMiddleware):
    """Reject requests without a valid X-API-Key header.

    Disabled when NEXUS_API_KEY is empty (development mode).
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        api_key = settings.NEXUS_API_KEY.strip()
        if not api_key:
            return await call_next(request)

        path = request.url.path
        if path in _EXEMPT_PATHS or path.startswith("/static"):
            return await call_next(request)

        if path.startswith("/dashboard"):
            if path == "/dashboard/login":
                return await call_next(request)
            if request.session.get("dashboard_authed"):
                return await call_next(request)
            from starlette.responses import RedirectResponse

            return RedirectResponse(url="/dashboard/login", status_code=302)

        provided = request.headers.get("X-API-Key", "")
        if not _safe_key_compare(provided, api_key):
            logger.warning(
                "Auth rejected: invalid API key from %s", request.client.host if request.client else "unknown"
            )
            return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key"})

        return await call_next(request)


_RATE_LIMITED_PATHS: set[str] = {
    "/api/agent/run",
    "/api/agent/stream",
    "/api/agent/compare",
    "/api/training/lora/compare",
    "/api/training/export",
    "/api/training/finetune",
    "/api/training/eval",
    "/api/training/promote-adapter",
    "/dashboard/login",
}

_RATE_LIMITED_PREFIXES: tuple[str, ...] = ("/api/traces/",)


def _is_rate_limited(path: str, method: str) -> bool:
    if method != "POST":
        return False
    if path in _RATE_LIMITED_PATHS:
        return True
    if path.startswith(_RATE_LIMITED_PREFIXES) and path.endswith("/re-evaluate"):
        return True
    return False


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window rate limiter per client IP.

    Applied to expensive POST endpoints: pipeline runs, LoRA comparisons,
    training exports, fine-tune triggers, and critic re-evaluations.
    Limit configurable via RATE_LIMIT_RPM. Disabled when RATE_LIMIT_RPM is 0.

    Backend: Redis (multi-worker) when REDIS_URL is set, otherwise in-process.
    """

    def __init__(self, app: ASGIApp, **kwargs: Any) -> None:
        super().__init__(app, **kwargs)
        self._lock = asyncio.Lock()
        global _rate_limiter_instance
        _rate_limiter_instance = self

    def reset(self) -> None:
        from app.services.rate_limiter import get_backend

        get_backend().reset()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        rpm = settings.RATE_LIMIT_RPM
        if rpm <= 0:
            return await call_next(request)

        if not _is_rate_limited(request.url.path, request.method):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"

        async with self._lock:
            from app.services.rate_limiter import get_backend

            backend = get_backend()
            if not backend.is_allowed(client_ip, rpm, 60):
                logger.warning("Rate limit exceeded for %s (%d RPM, backend=%s)", client_ip, rpm, backend.backend_type)
                return JSONResponse(
                    status_code=429,
                    content={"detail": f"Rate limit exceeded. Max {rpm} requests per minute."},
                    headers={"Retry-After": "60"},
                )

        return await call_next(request)
