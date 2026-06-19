"""add not_tradable_reason to epic

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
Create Date: 2026-06-12

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c0d1e2f3a4b5"
down_revision: str | None = "b9c0d1e2f3a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "epic",
        sa.Column("not_tradable_reason", sa.String(50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("epic", "not_tradable_reason")
