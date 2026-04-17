"""Security middleware: API key authentication, rate limiting, and observability."""

import hashlib
import hmac
import logging
import time
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


def _parse_api_keys() -> list[str]:
    """Parse comma-separated API keys from settings. First = primary."""
    raw = settings.NEXUS_API_KEY.strip()
    if not raw:
        return []
    return [k.strip() for k in raw.split(",") if k.strip()]


def check_api_key(provided: str) -> tuple[bool, bool]:
    """Check provided key against all configured keys.

    Returns (is_valid, is_primary). When only one key is configured,
    is_primary is always True on match. Comparison is timing-safe.
    """
    keys = _parse_api_keys()
    if not keys:
        return False, False
    for i, key in enumerate(keys):
        if _safe_key_compare(provided, key):
            return True, (i == 0)
    return False, False


_DASHBOARD_CSP = (
    "default-src 'none'; "
    "script-src 'none'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)
_API_CSP = "default-src 'none'; frame-ancestors 'none'"
_PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), interest-cohort=()"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers and API version to every response.

    Applies a strict CSP for dashboard HTML pages and a minimal CSP for API
    JSON responses. HSTS is added in non-dev environments. Every response
    includes ``X-API-Version`` for client version detection.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        from app.version import API_VERSION

        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        response.headers.setdefault("Permissions-Policy", _PERMISSIONS_POLICY)
        response.headers["X-API-Version"] = API_VERSION

        accept_version = request.headers.get("Accept-Version", "").strip()
        if accept_version and accept_version != API_VERSION:
            response.headers["X-API-Version-Mismatch"] = "true"

        is_html = request.url.path.startswith("/dashboard") or request.url.path == "/docs"
        csp = _DASHBOARD_CSP if is_html else _API_CSP
        response.headers.setdefault("Content-Security-Policy", csp)

        if settings.ENVIRONMENT.lower() not in ("development", "dev", "test"):
            response.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")

        return response


_rate_limiter_instance: "RateLimitMiddleware | None" = None


class AuthMiddleware(BaseHTTPMiddleware):
    """Reject requests without a valid X-API-Key header.

    Disabled when NEXUS_API_KEY is empty (development mode).
    Supports comma-separated keys for zero-downtime rotation:
    the first key is primary; subsequent keys are secondary (being
    rotated out). Secondary keys add X-API-Key-Deprecated: true.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        keys = _parse_api_keys()
        if not keys:
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
        valid, is_primary = check_api_key(provided)
        if not valid:
            logger.warning(
                "Auth rejected: invalid API key from %s", request.client.host if request.client else "unknown"
            )
            from app.errors import _build_error_body

            return JSONResponse(
                status_code=401,
                content=_build_error_body(401, "unauthorized", "Invalid or missing API key"),
            )

        response = await call_next(request)
        if not is_primary:
            response.headers["X-API-Key-Deprecated"] = "true"
        return response


_SHUTDOWN_EXEMPT = _EXEMPT_PATHS | {"/metrics"}


class ShutdownGuardMiddleware(BaseHTTPMiddleware):
    """Reject new requests during graceful shutdown and track in-flight count.

    Health, readiness, and metrics probes are always allowed through so that
    orchestrators (Kubernetes, ECS) can observe the drain state.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        from app.services.shutdown import get_coordinator

        coord = get_coordinator()

        path = request.url.path
        if path in _SHUTDOWN_EXEMPT or path.startswith("/static"):
            return await call_next(request)

        if coord.is_draining:
            from app.errors import _build_error_body

            return JSONResponse(
                status_code=503,
                content=_build_error_body(503, "shutting_down", "Server is shutting down"),
                headers={"Retry-After": "30"},
            )

        with coord.track_request():
            return await call_next(request)


_RATE_LIMITED_SUFFIXES: set[str] = {
    "/agent/run",
    "/agent/stream",
    "/agent/compare",
    "/training/lora/compare",
    "/training/export",
    "/training/finetune",
    "/training/eval",
    "/training/promote-adapter",
}

_RATE_LIMITED_PATHS: set[str] = {
    "/dashboard/login",
}
for _sfx in _RATE_LIMITED_SUFFIXES:
    _RATE_LIMITED_PATHS.add(f"/api{_sfx}")
    _RATE_LIMITED_PATHS.add(f"/v1{_sfx}")


def _is_rate_limited(path: str, method: str) -> bool:
    if method != "POST":
        return False
    if path in _RATE_LIMITED_PATHS:
        return True
    for prefix in ("/api/traces/", "/v1/traces/"):
        if path.startswith(prefix) and path.endswith("/re-evaluate"):
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

        from app.services.rate_limiter import get_backend

        backend = get_backend()
        result = backend.check(client_ip, rpm, 60)

        if not result.allowed:
            logger.warning(
                "Rate limit exceeded for %s (%d RPM, backend=%s)",
                client_ip,
                rpm,
                backend.backend_type,
            )
            from app.errors import _build_error_body

            retry = str(result.retry_after) if result.retry_after > 0 else "60"
            msg = f"Rate limit exceeded. Max {rpm} requests per minute."
            return JSONResponse(
                status_code=429,
                content=_build_error_body(429, "rate_limit_exceeded", msg),
                headers={
                    "Retry-After": retry,
                    "X-RateLimit-Limit": str(result.limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": retry,
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(result.limit)
        response.headers["X-RateLimit-Remaining"] = str(result.remaining)
        return response


_IDEMPOTENT_SUFFIXES: set[str] = {
    "/agent/run",
    "/agent/compare",
}

_IDEMPOTENT_PATHS: set[str] = set()
for _sfx in _IDEMPOTENT_SUFFIXES:
    _IDEMPOTENT_PATHS.add(f"/api{_sfx}")
    _IDEMPOTENT_PATHS.add(f"/v1{_sfx}")


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """Return cached responses for duplicate requests with the same Idempotency-Key.

    Only applies to POST requests on select expensive endpoints. On cache hit
    the response is replayed without re-executing the pipeline. The header
    ``X-Idempotent-Replayed: true`` signals that the response was served from
    cache.

    Keys that are too short (<8 chars) or too long (>256 chars) are rejected
    with 400 to prevent abuse.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method != "POST":
            return await call_next(request)

        if request.url.path not in _IDEMPOTENT_PATHS:
            return await call_next(request)

        key = request.headers.get("Idempotency-Key", "").strip()
        if not key:
            return await call_next(request)

        if len(key) < 8 or len(key) > 256:
            from app.errors import _build_error_body

            return JSONResponse(
                status_code=400,
                content=_build_error_body(
                    400,
                    "invalid_idempotency_key",
                    "Idempotency-Key must be 8–256 characters",
                ),
            )

        from app.services.idempotency import CachedResponse, get_store

        store = get_store()
        cached = store.get(key)
        if cached is not None:
            return Response(
                content=cached.body,
                status_code=cached.status_code,
                media_type=cached.content_type,
                headers={"X-Idempotent-Replayed": "true"},
            )

        if not store.acquire_inflight(key):
            from app.errors import _build_error_body

            return JSONResponse(
                status_code=409,
                content=_build_error_body(
                    409,
                    "duplicate_request",
                    "A request with this Idempotency-Key is already being processed",
                ),
                headers={"Retry-After": "2"},
            )

        try:
            response = await call_next(request)

            body = b""
            async for chunk in response.body_iterator:
                if isinstance(chunk, str):
                    body += chunk.encode("utf-8")
                else:
                    body += chunk

            content_type = response.headers.get("content-type", "application/json")
            store.set(
                key,
                CachedResponse(
                    status_code=response.status_code,
                    body=body,
                    content_type=content_type,
                    created_at=time.time(),
                ),
            )

            return Response(
                content=body,
                status_code=response.status_code,
                media_type=content_type,
                headers=dict(response.headers),
            )
        finally:
            store.release_inflight(key)


class LegacyApiDeprecationMiddleware(BaseHTTPMiddleware):
    """Add RFC 8594 Deprecation and Sunset headers to /api/ legacy routes.

    Signals clients that /api/ routes are deprecated in favour of /v1/.
    Includes a Link header pointing to the canonical /v1/ equivalent.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        path = request.url.path
        if not path.startswith("/api/"):
            return response

        response.headers["Deprecation"] = "true"
        sunset = settings.API_LEGACY_SUNSET.strip()
        if sunset:
            response.headers["Sunset"] = sunset

        v1_path = "/v1/" + path[len("/api/") :]
        response.headers.setdefault("Link", f'<{v1_path}>; rel="successor-version"')

        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Content-Length exceeds MAX_REQUEST_BODY_BYTES.

    Disabled when MAX_REQUEST_BODY_BYTES is 0. Checks the Content-Length
    header for an early reject before the body is read.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        limit = settings.MAX_REQUEST_BODY_BYTES
        if limit <= 0:
            return await call_next(request)

        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > limit:
                    from app.errors import _build_error_body

                    mb = limit / (1024 * 1024)
                    return JSONResponse(
                        status_code=413,
                        content=_build_error_body(
                            413,
                            "payload_too_large",
                            f"Request body exceeds {mb:.0f} MB limit",
                        ),
                    )
            except ValueError:
                pass

        return await call_next(request)


class MetricsMiddleware(BaseHTTPMiddleware):
    """Record HTTP request latency, counts, and in-flight gauge.

    Only active when prometheus_client is installed and EXPOSE_METRICS is true.
    Path labels are normalized to avoid high-cardinality explosion from IDs.
    DB pool gauges are updated on each request.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        from app.metrics import (
            HTTP_IN_FLIGHT,
            HTTP_REQUEST_LATENCY,
            HTTP_REQUESTS_TOTAL,
            normalize_path,
            update_db_pool_gauges,
        )

        HTTP_IN_FLIGHT.inc()
        start = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            HTTP_IN_FLIGHT.dec()
            raise

        elapsed = time.monotonic() - start
        HTTP_IN_FLIGHT.dec()

        status_code = response.status_code
        method = request.method
        path_label = normalize_path(request.url.path)
        status_class = f"{status_code // 100}xx"

        HTTP_REQUEST_LATENCY.labels(
            method=method,
            path_template=path_label,
            status_code=str(status_code),
        ).observe(elapsed)
        HTTP_REQUESTS_TOTAL.labels(method=method, status_class=status_class).inc()

        update_db_pool_gauges()

        return response
