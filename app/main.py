import logging
import os
import time
from contextlib import asynccontextmanager

from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from sqlalchemy import text

from app.config import settings
from app.db import Base, SessionLocal, engine
from app.api.agent import router as agent_router
from app.api.traces import router as traces_router
from app.api.critic import router as critic_router
from app.api.governance import router as governance_router
from app.api.training import router as training_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
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
            logger.warning("Alembic upgrade failed, falling back to create_all", exc_info=True)
    import app.models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    logger.info("Tables created via create_all (Alembic not available)")


def _seed_default_policies(db) -> None:
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
            priority="10",
        ),
        Policy(
            name="approve-file-write",
            description="File writes require approval",
            action_pattern="file_write",
            resource_pattern="*",
            decision="require_approval",
            risk_level="high",
            required_approvals="2",
            priority="50",
        ),
        Policy(
            name="approve-external-api",
            description="External API calls require approval",
            action_pattern="api_call",
            resource_pattern="external:*",
            decision="require_approval",
            risk_level="high",
            required_approvals="2",
            priority="50",
        ),
        Policy(
            name="deny-fund-transfer",
            description="Fund transfers are always denied by default",
            action_pattern="fund_transfer",
            resource_pattern="*",
            decision="deny",
            risk_level="critical",
            required_approvals="0",
            priority="1",
        ),
    ]

    for policy in defaults:
        db.add(policy)
    db.commit()
    logger.info("Seeded %d default governance policies", len(defaults))


def _seed_default_critics(db) -> None:
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _start_time
    _start_time = time.time()
    _run_migrations()

    db = SessionLocal()
    try:
        _seed_default_policies(db)
        _seed_default_critics(db)
    finally:
        db.close()

    from app.core.training.scheduler import start_scheduler, stop_scheduler
    start_scheduler()

    yield

    stop_scheduler()


from app.middleware import AuthMiddleware, RateLimitMiddleware

app = FastAPI(
    title=settings.PROJECT_NAME,
    version="0.1.0",
    description="Zero-Trust & Self-Evolving AI Agent System",
    lifespan=lifespan,
)

app.add_middleware(AuthMiddleware)
app.add_middleware(RateLimitMiddleware)


@app.get("/health")
def health_check():
    return {"status": "ok", "app": settings.PROJECT_NAME, "version": "0.1.0"}


@app.get("/health/ready")
def readiness_check():
    db_ok = False
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
        db_ok = True
    except Exception:
        logger.warning("Readiness: database unreachable", exc_info=True)

    uptime = round(time.time() - _start_time, 1) if _start_time else 0
    return {
        "status": "ready" if db_ok else "degraded",
        "database": "connected" if db_ok else "unreachable",
        "uptime_seconds": uptime,
    }


app.include_router(agent_router)
app.include_router(traces_router)
app.include_router(critic_router)
app.include_router(governance_router)
app.include_router(training_router)
