"""add skills table

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-04-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_episode_id", sa.String(), nullable=True),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("expected_reward", sa.Float(), nullable=True),
        sa.Column("min_reward_threshold", sa.Float(), nullable=True),
        sa.Column("total_runs", sa.Integer(), nullable=True),
        sa.Column("avg_reward", sa.Float(), nullable=True),
        sa.Column("last_reward", sa.Float(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("flagged", sa.Boolean(), nullable=True),
        sa.Column("skill_hash", sa.String(length=64), nullable=True),
        sa.Column("immune_scanned", sa.Boolean(), nullable=True),
        sa.Column("critic_scanned", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_skills_name", "skills", ["name"], unique=True)
    op.create_index("ix_skills_source_episode_id", "skills", ["source_episode_id"], unique=False)
    op.create_index("ix_skills_enabled", "skills", ["enabled"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_skills_enabled", table_name="skills")
    op.drop_index("ix_skills_source_episode_id", table_name="skills")
    op.drop_index("ix_skills_name", table_name="skills")
    op.drop_table("skills")
