"""mcp trace columns + skill import columns

Revision ID: d4e5f6a7b8c0
Revises: c3d4e5f6a7b8
Create Date: 2026-04-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c0"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    with op.batch_alter_table("traces", schema=None) as batch:
        batch.add_column(sa.Column("mcp_backend", sa.String(length=120), nullable=True))
        batch.add_column(sa.Column("mcp_tool_name", sa.String(length=200), nullable=True))
    if dialect == "postgresql":
        op.create_index("ix_traces_mcp_backend", "traces", ["mcp_backend"])
        op.create_index("ix_traces_mcp_tool_name", "traces", ["mcp_tool_name"])
    else:
        op.create_index("ix_traces_mcp_backend", "traces", ["mcp_backend"])
        op.create_index("ix_traces_mcp_tool_name", "traces", ["mcp_tool_name"])

    with op.batch_alter_table("skills", schema=None) as batch:
        batch.add_column(sa.Column("source", sa.String(length=100), nullable=True))
        batch.add_column(sa.Column("requirements", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("raw_source", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        op.drop_index("ix_traces_mcp_tool_name", table_name="traces")
        op.drop_index("ix_traces_mcp_backend", table_name="traces")
    else:
        op.drop_index("ix_traces_mcp_tool_name", table_name="traces")
        op.drop_index("ix_traces_mcp_backend", table_name="traces")

    with op.batch_alter_table("traces", schema=None) as batch:
        batch.drop_column("mcp_tool_name")
        batch.drop_column("mcp_backend")

    with op.batch_alter_table("skills", schema=None) as batch:
        batch.drop_column("raw_source")
        batch.drop_column("requirements")
        batch.drop_column("source")
