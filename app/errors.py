"""
Unified error handling: structured error responses and custom exceptions.

All API errors are normalized to a consistent envelope:
{
    "error": {
        "code": "rate_limit_exceeded",
        "message": "Rate limit exceeded. Max 30 requests per minute.",
        "status": 429,
        "request_id": "abc123",
        "trace_id": "00af7651..."   // when OTel active
    }
}

Pipeline responses (200 with status/error) are NOT changed — they follow the
pipeline contract. This module only normalizes HTTP error responses (4xx/5xx).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

_STATUS_CODE_MAP: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    429: "rate_limit_exceeded",
    500: "internal_error",
    502: "bad_gateway",
    503: "service_unavailable",
    504: "gateway_timeout",
}


class NexusAPIError(Exception):
    """Structured API error with machine-readable code.

    Raise this instead of HTTPException when you want a specific error code:
        raise NexusAPIError(400, "prompt_empty", "Prompt cannot be empty")
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


def _get_request_id() -> str:
    try:
        from app.logging_config import request_id_var

        return request_id_var.get("-")
    except Exception:
        return "-"


def _get_trace_id() -> str | None:
    try:
        from app.tracing import get_current_trace_context

        ctx = get_current_trace_context()
        return ctx["trace_id"] if ctx["trace_id"] != "-" else None
    except Exception:
        return None


def _build_error_body(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict:
    error_obj: dict[str, Any] = {
        "code": code,
        "message": message,
        "status": status_code,
        "request_id": _get_request_id(),
        "timestamp": datetime.now(UTC).isoformat(),
    }
    trace_id = _get_trace_id()
    if trace_id:
        error_obj["trace_id"] = trace_id
    if details:
        error_obj["details"] = details
    return {
        "error": error_obj,
        "detail": message,
    }


async def nexus_api_error_handler(request: Request, exc: NexusAPIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_build_error_body(exc.status_code, exc.code, exc.message, exc.details),
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _STATUS_CODE_MAP.get(exc.status_code, f"http_{exc.status_code}")
    message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=_build_error_body(exc.status_code, code, message),
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    field_errors = []
    for err in errors:
        loc = " → ".join(str(part) for part in err.get("loc", []))
        field_errors.append({"field": loc, "message": err.get("msg", ""), "type": err.get("type", "")})
    return JSONResponse(
        status_code=422,
        content=_build_error_body(
            422,
            "validation_error",
            "Request validation failed",
            {"fields": field_errors},
        ),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception: %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content=_build_error_body(500, "internal_error", "Internal server error"),
    )
