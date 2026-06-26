"""add level_margin to position

Revision ID: b3c4d5e6f7a8
Revises: d5e6f7a8b9c0
Create Date: 2026-06-22

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b3c4d5e6f7a8"
down_revision: str | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "position",
        sa.Column("level_margin", sa.Numeric(12, 5), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("position", "level_margin")
