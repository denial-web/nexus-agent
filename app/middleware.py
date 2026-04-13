"""Security middleware: API key authentication and rate limiting."""

import logging
import time
from collections import defaultdict

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config import settings

logger = logging.getLogger(__name__)

_EXEMPT_PATHS = {"/health", "/health/ready", "/docs", "/redoc", "/openapi.json", "/metrics"}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add baseline security headers to every response."""

    async def dispatch(self, request: Request, call_next) -> Response:
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

    async def dispatch(self, request: Request, call_next) -> Response:
        api_key = settings.NEXUS_API_KEY.strip()
        if not api_key:
            return await call_next(request)

        path = request.url.path
        if path in _EXEMPT_PATHS or path.startswith("/dashboard") or path.startswith("/static"):
            return await call_next(request)

        provided = request.headers.get("X-API-Key", "")
        if provided != api_key:
            logger.warning(
                "Auth rejected: invalid API key from %s", request.client.host if request.client else "unknown"
            )
            return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key"})

        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window rate limiter per client IP.

    Only applied to /api/agent/run. Limit configurable via RATE_LIMIT_RPM.
    Disabled when RATE_LIMIT_RPM is 0.
    """

    def __init__(self, app, **kwargs):
        super().__init__(app, **kwargs)
        self._requests: dict[str, list[float]] = defaultdict(list)
        global _rate_limiter_instance
        _rate_limiter_instance = self

    def reset(self) -> None:
        self._requests.clear()

    async def dispatch(self, request: Request, call_next) -> Response:
        rpm = settings.RATE_LIMIT_RPM
        if rpm <= 0:
            return await call_next(request)

        if request.url.path != "/api/agent/run" or request.method != "POST":
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        window_start = now - 60.0

        timestamps = self._requests[client_ip]
        self._requests[client_ip] = [t for t in timestamps if t > window_start]

        if len(self._requests[client_ip]) >= rpm:
            logger.warning("Rate limit exceeded for %s (%d/%d RPM)", client_ip, len(self._requests[client_ip]), rpm)
            return JSONResponse(
                status_code=429,
                content={"detail": f"Rate limit exceeded. Max {rpm} requests per minute."},
                headers={"Retry-After": "60"},
            )

        self._requests[client_ip].append(now)
        return await call_next(request)
