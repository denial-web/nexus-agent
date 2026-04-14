"""agent step_traces, episodes, trace agent columns

Revision ID: b2c3d4e5f6a7
Revises: 1a87a657bb74
Create Date: 2026-04-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "1a87a657bb74"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    with op.batch_alter_table("traces", schema=None) as batch_op:
        batch_op.add_column(sa.Column("run_mode", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("task_reward_score", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("user_feedback", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("total_steps", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("self_corrections", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("agent_state", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("agent_trajectory", sa.JSON(), nullable=True))

    op.create_table(
        "step_traces",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("trace_id", sa.String(), nullable=False),
        sa.Column("step_number", sa.Integer(), nullable=False),
        sa.Column("action_type", sa.String(length=32), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=True),
        sa.Column("tool_args", sa.JSON(), nullable=True),
        sa.Column("tool_result", sa.JSON(), nullable=True),
        sa.Column("covernor_decision", sa.String(length=32), nullable=True),
        sa.Column("critic_scores", sa.JSON(), nullable=True),
        sa.Column("reflection", sa.Text(), nullable=True),
        sa.Column("reward_signal", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["trace_id"], ["traces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_step_traces_trace_id", "step_traces", ["trace_id"], unique=False)

    op.create_table(
        "episodes",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("trace_id", sa.String(), nullable=True),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("task_summary", sa.Text(), nullable=False),
        sa.Column("tool_sequence", sa.JSON(), nullable=True),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("task_reward_score", sa.Float(), nullable=True),
        sa.Column("user_feedback", sa.String(length=20), nullable=True),
        sa.Column("reflection", sa.Text(), nullable=True),
        sa.Column("step_count", sa.Integer(), nullable=True),
        sa.Column("self_corrections", sa.Integer(), nullable=True),
        sa.Column("agent_trajectory", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_episodes_trace_id", "episodes", ["trace_id"], unique=False)
    op.create_index("ix_episodes_session_id", "episodes", ["session_id"], unique=False)

def downgrade() -> None:
    op.drop_index("ix_episodes_session_id", table_name="episodes")
    op.drop_index("ix_episodes_trace_id", table_name="episodes")
    op.drop_table("episodes")

    op.drop_index("ix_step_traces_trace_id", table_name="step_traces")
    op.drop_table("step_traces")

    with op.batch_alter_table("traces", schema=None) as batch_op:
        batch_op.drop_column("agent_trajectory")
        batch_op.drop_column("agent_state")
        batch_op.drop_column("self_corrections")
        batch_op.drop_column("total_steps")
        batch_op.drop_column("user_feedback")
        batch_op.drop_column("task_reward_score")
        batch_op.drop_column("run_mode")
