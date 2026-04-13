import os

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

os.environ["DATABASE_URL"] = "sqlite:///./test_nexus.db"
os.environ["NEXUS_API_KEY"] = ""
os.environ["GEMINI_API_KEY"] = ""
os.environ["OPENAI_API_KEY"] = ""
os.environ["DEEPSEEK_API_KEY"] = ""

from app.db import Base
from app.main import app
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def db_engine():
    if os.path.exists("test_nexus.db"):
        os.remove("test_nexus.db")
    engine = create_engine("sqlite:///./test_nexus.db", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragma(conn, _):
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    import app.models  # noqa: F401

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
    if os.path.exists("test_nexus.db"):
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
            priority="10",
        )
    )
    session.commit()


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
