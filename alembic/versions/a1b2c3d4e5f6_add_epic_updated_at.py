"""Add epic.updated_at for epic-list persistence

Revision ID: a1b2c3d4e5f6
Revises: 036f1961c018
Create Date: 2026-06-03 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "036f1961c018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the timestamp column tracking the last navigation-tree crawl."""
    with op.batch_alter_table("epic", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    """Drop the timestamp column."""
    with op.batch_alter_table("epic", schema=None) as batch_op:
        batch_op.drop_column("updated_at")
