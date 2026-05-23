"""Add full-record audit hash to traces.

Revision ID: aa11bb22cc33
Revises: d3cf357233d3
Create Date: 2026-05-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "aa11bb22cc33"
down_revision: str | None = "d3cf357233d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("traces", schema=None) as batch_op:
        batch_op.add_column(sa.Column("full_record_hash", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("traces", schema=None) as batch_op:
        batch_op.drop_column("full_record_hash")
