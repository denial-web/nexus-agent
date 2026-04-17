import logging
import os

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

logging.raiseExceptions = False

_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "sqlite:///./test_nexus.db")
_IS_SQLITE = _TEST_DB_URL.startswith("sqlite")

os.environ["DATABASE_URL"] = _TEST_DB_URL
os.environ["NEXUS_API_KEY"] = ""
os.environ["GEMINI_API_KEY"] = ""
os.environ["OPENAI_API_KEY"] = ""
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["EXPOSE_METRICS"] = "true"

from app.db import Base  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def db_engine():
    connect_args: dict = {}
    if _IS_SQLITE:
        if os.path.exists("test_nexus.db"):
            os.remove("test_nexus.db")
        connect_args["check_same_thread"] = False

    engine = create_engine(_TEST_DB_URL, connect_args=connect_args)

    if _IS_SQLITE:

        @event.listens_for(engine, "connect")
        def _pragma(conn, _):
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    import app.models  # noqa: F401

    if not _IS_SQLITE:
        Base.metadata.drop_all(bind=engine)

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        from app.main import _seed_default_critics

        _seed_default_critics(s)
    finally:
        s.close()
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()
    if _IS_SQLITE and os.path.exists("test_nexus.db"):
        os.remove("test_nexus.db")


@pytest.fixture
def db_session(db_engine):
    Session = sessionmaker(bind=db_engine)
    session = Session()
    _seed_test_policies(session)
    yield session
    session.rollback()
    session.close()


def _seed_test_policies(session):
    from app.models.policy import Policy

    if session.query(Policy).count() > 0:
        return
    session.add(
        Policy(
            name="allow-chat-respond",
            action_pattern="respond",
            resource_pattern="chat",
            decision="allow",
            risk_level="low",
            required_approvals="0",
            priority=10,
        )
    )
    session.commit()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Clear rate-limiter state before every test to prevent cross-test 429s."""
    from app.middleware import _rate_limiter_instance

    if _rate_limiter_instance is not None:
        _rate_limiter_instance.reset()
    yield
    if _rate_limiter_instance is not None:
        _rate_limiter_instance.reset()


@pytest.fixture(autouse=True)
def _reset_shutdown_coordinator():
    """Ensure shutdown coordinator is fresh for every test."""
    from app.services.shutdown import reset_coordinator

    reset_coordinator()
    yield
    reset_coordinator()


@pytest.fixture(autouse=True)
def _drain_webhook_pool():
    """Drain the webhook dispatcher thread pool at the end of every test so
    background deliveries don't outlive pytest's per-test stdout/stderr
    capture and surface as noisy 'I/O operation on closed file' errors."""
    yield
    from app.services.webhooks import shutdown_pool

    shutdown_pool(wait=True, timeout=5.0)


@pytest.fixture
def client(db_engine):
    from app.db import get_db

    Session = sessionmaker(bind=db_engine)

    def _override():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
