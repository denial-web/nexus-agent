"""Migrate DateTime columns to TIMESTAMPTZ for timezone portability

Revision ID: a7bfd492ce12
Revises: a8efac170259
Create Date: 2026-04-17 11:39:37.151635

Why:
    Every DateTime column in the schema was declared as naive (no timezone).
    On PostgreSQL this maps to ``TIMESTAMP WITHOUT TIME ZONE``, which silently
    shifts tz-aware Python datetimes by the server's local timezone on insert.
    A server running in Asia/Phnom_Penh would offset every ``expires_at``,
    ``created_at``, etc. by 7 hours, breaking expiry checks and retention.

    ``app/db.py`` already pins every session to UTC as defense in depth, but
    column-level timezone awareness is the proper fix — self-documenting,
    dialect-agnostic, and immune to operator misconfiguration.

How the ALTER is safe:
    Existing rows on PostgreSQL are stored as naive UTC values (because every
    connection pins ``SET TIME ZONE 'UTC'``). When PostgreSQL ALTERs a
    ``TIMESTAMP`` column to ``TIMESTAMPTZ``, it interprets existing naive
    values in the current session's timezone — UTC — so values round-trip
    correctly without a manual ``USING`` cast.

    On SQLite the conversion is effectively a no-op (dates are stored as ISO
    strings in either case). We wrap each ALTER in ``batch_alter_table`` so
    SQLite can execute the change via the copy-and-rename strategy while
    PostgreSQL emits a native ``ALTER COLUMN ... TYPE TIMESTAMPTZ`` statement.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7bfd492ce12"
down_revision: str | None = "a8efac170259"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_COLUMNS_BY_TABLE: list[tuple[str, list[tuple[str, bool]]]] = [
    ("approval_requests", [("expires_at", True), ("resolved_at", True), ("created_at", True)]),
    ("approval_votes", [("created_at", True)]),
    ("calibration_snapshots", [("recorded_at", False)]),
    ("critic_registry", [("created_at", True), ("updated_at", True)]),
    ("doctrine_outbox", [("next_retry_at", True), ("created_at", True), ("updated_at", True)]),
    ("episodes", [("created_at", True)]),
    ("labeling_queue", [("exported_at", True), ("created_at", True), ("labeled_at", True)]),
    ("policies", [("created_at", True), ("updated_at", True)]),
    ("skills", [("created_at", True), ("updated_at", True)]),
    ("step_traces", [("created_at", True)]),
    ("traces", [("created_at", True)]),
    ("webhooks", [("last_triggered_at", True), ("created_at", True), ("updated_at", True)]),
]


def _alter(old: sa.types.TypeEngine, new: sa.types.TypeEngine) -> None:
    for table, cols in _COLUMNS_BY_TABLE:
        with op.batch_alter_table(table) as batch:
            for column, nullable in cols:
                batch.alter_column(
                    column,
                    existing_type=old,
                    type_=new,
                    existing_nullable=nullable,
                )


def upgrade() -> None:
    _alter(sa.DateTime(), sa.DateTime(timezone=True))


def downgrade() -> None:
    _alter(sa.DateTime(timezone=True), sa.DateTime())
