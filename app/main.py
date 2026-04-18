import hashlib
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse, Response

from app.api.agent import router as agent_router
from app.api.critic import router as critic_router
from app.api.dashboard import router as dashboard_router
from app.api.governance import router as governance_router
from app.api.mcp import router as mcp_router
from app.api.skills import router as skills_router
from app.api.traces import router as traces_router
from app.api.training import router as training_router
from app.api.webhooks import router as webhooks_router
from app.config import settings
from app.db import Base, SessionLocal, engine
from app.errors import (
    NexusAPIError,
    http_exception_handler,
    nexus_api_error_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from app.logging_config import configure_logging, request_id_var
from app.middleware import (
    AuthMiddleware,
    BodySizeLimitMiddleware,
    IdempotencyMiddleware,
    LegacyApiDeprecationMiddleware,
    MetricsMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    ShutdownGuardMiddleware,
)

configure_logging()
logger = logging.getLogger(__name__)

_start_time: float = 0.0


def _run_migrations() -> None:
    alembic_cfg_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic.ini")
    if os.path.isfile(alembic_cfg_path):
        cfg = AlembicConfig(alembic_cfg_path)
        cfg.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
        try:
            command.upgrade(cfg, "head")
            logging.getLogger().setLevel(logging.INFO)
            logger.info("Alembic migrations applied successfully")
            return
        except Exception:
            if settings.ENVIRONMENT.lower() not in ("development", "dev", "test"):
                logger.error("Alembic upgrade failed in production — refusing to fall back to create_all")
                raise
            logger.warning("Alembic upgrade failed, falling back to create_all", exc_info=True)
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    logger.info("Tables created via create_all (Alembic not available)")


def _seed_default_policies(db: Session) -> None:
    """Seed default governance policies if none exist."""
    from app.models.policy import Policy

    if db.query(Policy).count() > 0:
        return

    defaults = [
        Policy(
            name="allow-chat-respond",
            description="Allow basic chat responses",
            action_pattern="respond",
            resource_pattern="chat",
            decision="allow",
            risk_level="low",
            required_approvals="0",
            priority=10,
        ),
        Policy(
            name="approve-file-write",
            description="File writes require approval",
            action_pattern="file_write",
            resource_pattern="*",
            decision="require_approval",
            risk_level="high",
            required_approvals="2",
            priority=50,
        ),
        Policy(
            name="approve-external-api",
            description="External API calls require approval",
            action_pattern="api_call",
            resource_pattern="external:*",
            decision="require_approval",
            risk_level="high",
            required_approvals="2",
            priority=50,
        ),
        Policy(
            name="deny-fund-transfer",
            description="Fund transfers are always denied by default",
            action_pattern="fund_transfer",
            resource_pattern="*",
            decision="deny",
            risk_level="critical",
            required_approvals="0",
            priority=1,
        ),
    ]

    for policy in defaults:
        db.add(policy)
    db.commit()
    logger.info("Seeded %d default governance policies", len(defaults))


def _seed_agent_policies(db: Session) -> None:
    """Add governed tool policies if missing (idempotent by name)."""
    from app.models.policy import Policy

    wanted = [
        Policy(
            name="agent-allow-file-read",
            description="Agent file reads under workspace",
            action_pattern="file_read",
            resource_pattern="*",
            decision="allow",
            risk_level="low",
            required_approvals="0",
            priority=8,
        ),
        Policy(
            name="agent-allow-file-write",
            description="Agent file writes under workspace",
            action_pattern="file_write",
            resource_pattern="*",
            decision="allow",
            risk_level="medium",
            required_approvals="0",
            priority=8,
        ),
        Policy(
            name="agent-shell-destructive-approval",
            description="Destructive shell patterns require approval",
            action_pattern="shell_exec",
            resource_pattern="*rm*",
            decision="require_approval",
            risk_level="high",
            required_approvals="1",
            priority=3,
        ),
        Policy(
            name="agent-shell-sudo-approval",
            description="sudo requires approval",
            action_pattern="shell_exec",
            resource_pattern="*sudo*",
            decision="require_approval",
            risk_level="high",
            required_approvals="1",
            priority=3,
        ),
        Policy(
            name="agent-shell-allow",
            description="General shell under governance",
            action_pattern="shell_exec",
            resource_pattern="*",
            decision="allow",
            risk_level="medium",
            required_approvals="0",
            priority=40,
        ),
        Policy(
            name="agent-web-fetch-deny-internal",
            description="Block agent from fetching internal/localhost URLs",
            action_pattern="web_fetch",
            resource_pattern="*localhost*",
            decision="deny",
            risk_level="high",
            required_approvals="0",
            priority=5,
        ),
        Policy(
            name="agent-web-fetch-deny-internal-ip",
            description="Block agent from fetching 127.0.0.1 URLs",
            action_pattern="web_fetch",
            resource_pattern="*127.0.0.1*",
            decision="deny",
            risk_level="high",
            required_approvals="0",
            priority=5,
        ),
        Policy(
            name="agent-web-fetch-allow",
            description="HTTP fetch for agent",
            action_pattern="web_fetch",
            resource_pattern="*",
            decision="allow",
            risk_level="medium",
            required_approvals="0",
            priority=15,
        ),
        Policy(
            name="agent-search-allow",
            description="Web search for agent",
            action_pattern="search",
            resource_pattern="*",
            decision="allow",
            risk_level="medium",
            required_approvals="0",
            priority=15,
        ),
    ]
    for p in wanted:
        if db.query(Policy).filter_by(name=p.name).first() is None:
            db.add(p)
    db.commit()


def _seed_mcp_policies(db: Session) -> None:
    """Default-deny all MCP tool actions (idempotent by policy name)."""
    from app.models.policy import Policy

    if db.query(Policy).filter_by(name="mcp-default-deny").first() is not None:
        return
    db.add(
        Policy(
            name="mcp-default-deny",
            description="Deny all MCP proxy tool calls until explicitly allowed",
            action_pattern="mcp:*",
            resource_pattern="*",
            decision="deny",
            risk_level="high",
            required_approvals="0",
            priority=100,
        )
    )
    db.commit()
    logger.info("Seeded MCP default-deny policy")


def _seed_memory_policies(db: Session) -> None:
    """Seed default Covernor policies for the `memory:*` namespace.

    Phase 12A ships ONE scoped allow policy: `memory:write:preference` is
    permitted for any entity. All other `memory:write:*` actions fall
    through to the namespace-wide default-deny. When `MEMORY_ENABLED` is
    False the policies are still seeded so the regression tripwire stays
    strict, but `app.core.memory.writer.write_belief` returns early and
    never reaches the policy engine.

    Idempotent by policy name.
    """
    from app.models.policy import Policy

    existing = {
        row.name
        for row in db.query(Policy.name)
        .filter(Policy.name.in_(["memory-default-deny", "memory-allow-preference-write"]))
        .all()
    }

    wanted = [
        Policy(
            name="memory-default-deny",
            description="Deny all memory writes until explicitly allowed",
            action_pattern="memory:write:*",
            resource_pattern="*",
            decision="deny",
            risk_level="high",
            required_approvals="0",
            priority=100,
        ),
        Policy(
            name="memory-allow-preference-write",
            description="Allow governed writes of user preference beliefs",
            action_pattern="memory:write:preference",
            resource_pattern="*",
            decision="allow",
            risk_level="low",
            required_approvals="0",
            priority=10,
        ),
    ]

    added = 0
    for policy in wanted:
        if policy.name in existing:
            continue
        db.add(policy)
        added += 1
    if added:
        db.commit()
        logger.info("Seeded %d memory policies", added)


def _seed_default_critics(db: Session) -> None:
    """Seed default critic_registry rows if the table is empty."""
    from app.models.critic_registry import CriticNode

    if db.query(CriticNode).count() > 0:
        return

    defaults = [
        CriticNode(
            name="reasoning",
            node_type="reasoning",
            description="Reasoning quality — LLM deep check with heuristic pre-filter",
            prompt_template=(
                "Evaluate logical coherence and structure of the model response.\n\n"
                "User prompt:\n{prompt}\n\nModel response:\n{response}"
            ),
            threshold_pass=0.7,
            threshold_halt=0.3,
            can_halt=False,
            weight=1.0,
        ),
        CriticNode(
            name="injection",
            node_type="injection",
            description="Injection / leak detection — LLM deep check with heuristic pre-filter",
            prompt_template=(
                "Assess whether the model output leaks system instructions or exhibits "
                "prompt-injection behavior.\n\nUser prompt:\n{prompt}\n\nModel response:\n{response}"
            ),
            threshold_pass=0.7,
            threshold_halt=0.3,
            can_halt=True,
            weight=1.0,
        ),
        CriticNode(
            name="safety",
            node_type="safety",
            description="Fast heuristic safety patterns",
            prompt_template=None,
            threshold_pass=0.7,
            threshold_halt=0.3,
            can_halt=True,
            weight=1.0,
        ),
        CriticNode(
            name="quality",
            node_type="quality",
            description="Output quality heuristics",
            prompt_template=None,
            threshold_pass=0.6,
            threshold_halt=0.3,
            can_halt=False,
            weight=1.0,
        ),
    ]

    for node in defaults:
        db.add(node)
    db.commit()
    logger.info("Seeded %d default critic registry nodes", len(defaults))


def _validate_startup_config() -> None:
    """Run comprehensive config validation; abort on fatal errors."""
    from app.services.config_validator import validate

    issues = validate(settings)
    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warning"]

    for w in warnings:
        logger.warning("Config: %s", w.message)
    for e in errors:
        logger.error("Config: %s", e.message)

    if errors:
        summary = "; ".join(e.message for e in errors)
        raise RuntimeError(f"Fatal configuration errors ({len(errors)}): {summary}")


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Assign a unique request ID and propagate OTel trace context."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = rid
            try:
                from app.tracing import get_current_trace_context

                ctx = get_current_trace_context()
                if ctx["trace_id"] != "-":
                    response.headers["X-Trace-ID"] = ctx["trace_id"]
            except Exception:
                pass
            return response
        finally:
            request_id_var.reset(token)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _start_time
    _start_time = time.time()
    _validate_startup_config()
    _run_migrations()

    async with AsyncExitStack() as stack:
        db = SessionLocal()
        try:
            _seed_default_policies(db)
            _seed_agent_policies(db)
            _seed_mcp_policies(db)
            _seed_memory_policies(db)
            _seed_default_critics(db)
        finally:
            db.close()

        if settings.MCP_ENABLED and not settings.LOCAL_ONLY:
            from app.core.mcp.server import get_streamable_http_app

            mcp_starlette = get_streamable_http_app("/mcp")
            await stack.enter_async_context(mcp_starlette.router.lifespan_context(mcp_starlette))

        from app.channels.telegram_bot import start_telegram_polling_if_configured

        start_telegram_polling_if_configured()

        from app.core.training.scheduler import start_scheduler, stop_scheduler

        _skip_scheduler = os.environ.get("NEXUS_SKIP_SCHEDULER", "").strip().lower() in ("1", "true", "yes")
        if _skip_scheduler:
            logger.info("Scheduler disabled via NEXUS_SKIP_SCHEDULER — this worker will not run background jobs")
        else:
            start_scheduler()

        from app.tracing import init_tracing, shutdown_tracing

        init_tracing()

        yield

        from app.services.shutdown import get_coordinator

        coord = get_coordinator()
        coord.start_drain()
        await coord.wait_for_drain(settings.SHUTDOWN_DRAIN_SECONDS)

        if not _skip_scheduler:
            stop_scheduler()
        shutdown_tracing()


app = FastAPI(
    title=settings.PROJECT_NAME,
    version="0.1.0",
    description="Zero-Trust & Self-Evolving AI Agent System",
    lifespan=lifespan,
)

_cors = settings.CORS_ORIGINS.strip()
if _cors:
    _origins = [o.strip() for o in _cors.split(",") if o.strip()]
    if _origins:
        _methods = [m.strip() for m in settings.CORS_ALLOW_METHODS.split(",") if m.strip()]
        _headers = [h.strip() for h in settings.CORS_ALLOW_HEADERS.split(",") if h.strip()]
        _creds = "*" not in _origins
        app.add_middleware(
            CORSMiddleware,
            allow_origins=_origins,
            allow_credentials=_creds,
            allow_methods=_methods or ["GET"],
            allow_headers=_headers or ["Content-Type"],
            max_age=settings.CORS_MAX_AGE,
        )

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(LegacyApiDeprecationMiddleware)
app.add_middleware(IdempotencyMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(ShutdownGuardMiddleware)
if settings.EXPOSE_METRICS:
    app.add_middleware(MetricsMiddleware)
app.add_middleware(RequestIdMiddleware)


@app.middleware("http")
async def mcp_local_only_guard(request: Request, call_next: RequestResponseEndpoint) -> Response:
    if settings.LOCAL_ONLY and request.url.path.startswith("/mcp"):
        from app.errors import _build_error_body

        return JSONResponse(
            status_code=503,
            content=_build_error_body(503, "service_unavailable", "MCP proxy disabled in LOCAL_ONLY mode"),
        )
    return await call_next(request)


def _session_secret_key() -> bytes:
    if not settings.SESSION_SECRET.strip():
        if settings.ENVIRONMENT.lower() not in ("development", "dev", "test"):
            raise RuntimeError(
                "SESSION_SECRET must be set in non-development environments. "
                'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
    return hashlib.sha256(settings.get_session_secret().encode()).digest()


app.add_middleware(SessionMiddleware, secret_key=_session_secret_key())


app.add_exception_handler(NexusAPIError, nexus_api_error_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)


if settings.EXPOSE_METRICS:
    try:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        @app.get("/metrics")
        def metrics() -> Response:
            return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
    except ImportError:
        logger.warning("prometheus_client not installed; /metrics disabled")


@app.get("/health")
def health_check() -> Response:
    from app.services.shutdown import get_coordinator

    coord = get_coordinator()
    if coord.is_draining:
        return JSONResponse(
            status_code=503,
            content={"status": "draining", "app": settings.PROJECT_NAME, "version": "0.1.0"},
        )
    return JSONResponse(
        status_code=200,
        content={"status": "ok", "app": settings.PROJECT_NAME, "version": "0.1.0"},
    )


@app.get("/health/ready")
def readiness_check(deep: bool = False) -> Response:
    checks: dict = {}
    overall_ok = True

    db_ok = False
    try:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            db_ok = True
        finally:
            db.close()
    except Exception:
        logger.warning("Readiness: database unreachable", exc_info=True)
    db_check: dict[str, Any] = {"status": "connected" if db_ok else "unreachable"}
    try:
        pool = engine.pool
        if hasattr(pool, "size"):
            db_check["pool_size"] = pool.size()
            db_check["pool_checked_in"] = pool.checkedin()
            db_check["pool_checked_out"] = pool.checkedout()
            db_check["pool_overflow"] = pool.overflow()
    except Exception:
        pass
    checks["database"] = db_check
    if not db_ok:
        overall_ok = False

    try:
        from app.core.llm.provider import get_available_providers

        providers = get_available_providers()
        checks["llm_providers"] = len(providers)
        checks["llm_provider_names"] = [p["provider"] for p in providers]
    except Exception:
        checks["llm_providers"] = 0
        checks["llm_provider_names"] = []

    try:
        from app.core.llm.circuit_breaker import get_registry

        cb_status = get_registry().get_all_status()
        open_circuits = [name for name, info in cb_status.items() if info.get("state") == "open"]
        checks["circuit_breakers"] = {
            "total": len(cb_status),
            "open": open_circuits,
        }
    except Exception:
        checks["circuit_breakers"] = {"total": 0, "open": []}

    try:
        from app.core.llm.cache import get_cache

        cache = get_cache()
        stats = cache.stats()
        checks["llm_cache"] = {
            "enabled": stats.get("enabled", False),
            "size": stats.get("size", 0),
            "hit_rate": stats.get("hit_rate", 0.0),
        }
    except Exception:
        checks["llm_cache"] = {"enabled": False}

    try:
        from app.tracing import is_available, is_enabled

        checks["tracing"] = {"enabled": is_enabled(), "available": is_available()}
    except Exception:
        checks["tracing"] = {"enabled": False, "available": False}

    try:
        from app.services.rate_limiter import get_status as rl_status

        checks["rate_limiter"] = rl_status()
    except Exception:
        checks["rate_limiter"] = {"backend_type": "unknown"}

    checks["webhooks_enabled"] = settings.WEBHOOKS_ENABLED
    checks["mcp_enabled"] = settings.MCP_ENABLED

    try:
        from app.services.shutdown import get_coordinator

        coord = get_coordinator()
        checks["shutdown"] = {
            "draining": coord.is_draining,
            "in_flight": coord.in_flight,
        }
        if coord.is_draining:
            overall_ok = False
    except Exception:
        checks["shutdown"] = {"draining": False, "in_flight": 0}

    if deep:
        try:
            from app.services.health_probe import probe_providers

            provider_probes = probe_providers()
            unreachable = [name for name, info in provider_probes.items() if not info.get("reachable")]
            checks["provider_probes"] = provider_probes
            if unreachable:
                checks["provider_probes_warning"] = f"Unreachable providers: {', '.join(unreachable)}"
        except Exception:
            checks["provider_probes"] = {"error": "probe failed"}

    uptime = round(time.time() - _start_time, 1) if _start_time else 0
    status_label = "ready" if overall_ok else "degraded"
    body = {
        "status": status_label,
        "uptime_seconds": uptime,
        "checks": checks,
    }
    return JSONResponse(content=body, status_code=200 if overall_ok else 503)


app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")

_api_routers = [
    agent_router,
    traces_router,
    critic_router,
    governance_router,
    skills_router,
    mcp_router,
    training_router,
    webhooks_router,
]

v1_router = APIRouter(prefix="/v1")
legacy_router = APIRouter(prefix="/api")
for _r in _api_routers:
    v1_router.include_router(_r)
    legacy_router.include_router(_r)

app.include_router(v1_router)
app.include_router(legacy_router)
app.include_router(dashboard_router)

if settings.MCP_ENABLED and not settings.LOCAL_ONLY:
    from app.core.mcp.server import get_streamable_http_app

    app.mount("/mcp", get_streamable_http_app("/mcp"))
