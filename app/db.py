import logging
import sqlite3
from collections.abc import Generator
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)

_is_sqlite = settings.DATABASE_URL.startswith("sqlite")


@event.listens_for(Engine, "connect")
def _pin_postgres_utc(dbapi_conn: Any, connection_record: Any) -> None:
    """Pin every PostgreSQL connection's session timezone to UTC.

    Applied at the Engine base class so tests (which create their own engine)
    and any embedded engines inherit it. Without this, a server running in a
    non-UTC timezone (e.g. Asia/Phnom_Penh) silently shifts tz-aware datetimes
    by the server offset when they land in TIMESTAMP WITHOUT TIME ZONE
    columns, breaking every expires_at/created_at comparison in the app.

    Note: psycopg2 opens an implicit transaction on the first statement, and
    SQLAlchemy's pool reset issues ROLLBACK on connection return — which
    would undo a plain SET. Committing here promotes the SET to a permanent
    session setting that survives subsequent rollbacks.
    """
    cls_name = type(dbapi_conn).__module__
    if "psycopg" not in cls_name:
        return
    cursor = dbapi_conn.cursor()
    try:
        cursor.execute("SET TIME ZONE 'UTC'")
    finally:
        cursor.close()
    dbapi_conn.commit()


def _build_engine_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {}

    if _is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_size"] = settings.DB_POOL_SIZE
        kwargs["max_overflow"] = settings.DB_MAX_OVERFLOW
        kwargs["pool_timeout"] = settings.DB_POOL_TIMEOUT
        kwargs["pool_pre_ping"] = settings.DB_POOL_PRE_PING
        if settings.DB_POOL_RECYCLE > 0:
            kwargs["pool_recycle"] = settings.DB_POOL_RECYCLE

    return kwargs


engine = create_engine(settings.DATABASE_URL, **_build_engine_kwargs())

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn: sqlite3.Connection, connection_record: Any) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db() -> Generator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
