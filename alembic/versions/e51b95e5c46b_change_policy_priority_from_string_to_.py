"""change policy priority from string to integer

Revision ID: e51b95e5c46b
Revises: 4dca71e6e14e
Create Date: 2026-04-13 11:40:27.561377

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e51b95e5c46b"
down_revision: str | None = "4dca71e6e14e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("policies", schema=None) as batch_op:
            batch_op.alter_column("priority", existing_type=sa.VARCHAR(), type_=sa.Integer(), existing_nullable=False)
    else:
        op.execute("ALTER TABLE policies ALTER COLUMN priority TYPE INTEGER USING priority::integer")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("policies", schema=None) as batch_op:
            batch_op.alter_column("priority", existing_type=sa.Integer(), type_=sa.VARCHAR(), existing_nullable=False)
    else:
        op.execute("ALTER TABLE policies ALTER COLUMN priority TYPE VARCHAR USING priority::varchar")
