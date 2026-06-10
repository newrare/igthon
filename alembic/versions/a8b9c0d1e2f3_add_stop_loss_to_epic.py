"""add stop_loss to epic

Revision ID: a8b9c0d1e2f3
Revises: a7b8c9d0e1f2
Create Date: 2026-06-09

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a8b9c0d1e2f3"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "epic",
        sa.Column("stop_loss", sa.Numeric(10, 3), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("epic", "stop_loss")
