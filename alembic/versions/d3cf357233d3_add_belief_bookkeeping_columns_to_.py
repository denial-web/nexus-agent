"""Add belief bookkeeping columns to traces and episodes.

Phase 12 Week 2: store which beliefs a run retrieved (`beliefs_used`)
and which it formed (`beliefs_formed`) as JSON arrays of belief ids.
Additive-only — both columns are nullable so rows predating the memory
subsystem remain valid and indistinguishable from rows written with
`MEMORY_ENABLED=False`.

Revision ID: d3cf357233d3
Revises: 8a4579763b4d
Create Date: 2026-04-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3cf357233d3"
down_revision: str | None = "8a4579763b4d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("episodes", schema=None) as batch_op:
        batch_op.add_column(sa.Column("beliefs_used", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("beliefs_formed", sa.JSON(), nullable=True))

    with op.batch_alter_table("traces", schema=None) as batch_op:
        batch_op.add_column(sa.Column("beliefs_used", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("beliefs_formed", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("traces", schema=None) as batch_op:
        batch_op.drop_column("beliefs_formed")
        batch_op.drop_column("beliefs_used")

    with op.batch_alter_table("episodes", schema=None) as batch_op:
        batch_op.drop_column("beliefs_formed")
        batch_op.drop_column("beliefs_used")
