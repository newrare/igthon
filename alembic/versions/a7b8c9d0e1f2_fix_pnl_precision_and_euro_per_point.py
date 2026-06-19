"""widen price levels to Numeric(12,5) and add euro_per_point

Forex pairs move below the 3rd decimal (e.g. GBP/EUR at 1.15729), so the
previous Numeric(10, 3) columns truncated the whole price move to zero and the
euro P&L came out as 0.00. Widen every price level to 5 decimals and add an
``euro_per_point`` column that stores the currency-converted euro value of a
single point of movement for the position.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-06-08

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Price/level columns widened from (10, 3) to (12, 5).
_LEVEL_COLUMNS = (
    "level_follower",
    "level_win",
    "level_zero",
    "level_open",
    "level_loose",
    "level_security",
    "level_close",
    "level_stop",
    "pip_spread",
)


def upgrade() -> None:
    # Batch mode keeps this portable: SQLite (dev) has no ALTER COLUMN TYPE, so
    # Alembic recreates the table copying existing rows; PostgreSQL (prod) emits
    # a plain ALTER COLUMN.
    with op.batch_alter_table("position") as batch:
        for column in _LEVEL_COLUMNS:
            batch.alter_column(
                column,
                type_=sa.Numeric(12, 5),
                existing_type=sa.Numeric(10, 3),
                existing_nullable=True,
            )
        batch.add_column(sa.Column("euro_per_point", sa.Numeric(14, 6), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("position") as batch:
        batch.drop_column("euro_per_point")
        for column in _LEVEL_COLUMNS:
            batch.alter_column(
                column,
                type_=sa.Numeric(10, 3),
                existing_type=sa.Numeric(12, 5),
                existing_nullable=True,
            )
