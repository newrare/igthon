"""add direction to position

Revision ID: a1c2e3f4d5b6
Revises: a9b8c7d6e5f4
Create Date: 2026-07-02

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1c2e3f4d5b6"
down_revision: str | None = "a9b8c7d6e5f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing rows are all long positions: server_default 'BUY' backfills them.
    op.add_column(
        "position",
        sa.Column(
            "direction",
            sa.String(length=4),
            nullable=False,
            server_default="BUY",
        ),
    )


def downgrade() -> None:
    op.drop_column("position", "direction")
