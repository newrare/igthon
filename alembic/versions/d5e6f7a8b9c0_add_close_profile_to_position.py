"""add close_profile to position

Records which CloseProfile manages a position's exit, decoupling the exit
scenario from the entry strategy. Existing open rows are backfilled to
"atr_trailing" (the reference profile that reproduces prior behaviour).

Revision ID: d5e6f7a8b9c0
Revises: c0d1e2f3a4b5
Create Date: 2026-06-18

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision: str | None = "c0d1e2f3a4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "position",
        sa.Column("close_profile", sa.String(length=30), nullable=True),
    )
    # Backfill existing rows so the monitor keeps managing them with the
    # behaviour they were opened under.
    op.execute(
        "UPDATE position SET close_profile = 'atr_trailing' "
        "WHERE close_profile IS NULL"
    )


def downgrade() -> None:
    op.drop_column("position", "close_profile")
