"""Add candle table for persisted price history (charts + backtesting)

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-05 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the candle table with a dedup constraint and lookup index."""
    op.create_table(
        "candle",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("epic", sa.String(length=30), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bid_open", sa.Float(), nullable=False),
        sa.Column("bid_close", sa.Float(), nullable=False),
        sa.Column("bid_high", sa.Float(), nullable=False),
        sa.Column("bid_low", sa.Float(), nullable=False),
        sa.Column("offer_open", sa.Float(), nullable=False),
        sa.Column("offer_close", sa.Float(), nullable=False),
        sa.Column("offer_high", sa.Float(), nullable=False),
        sa.Column("offer_low", sa.Float(), nullable=False),
        sa.Column("volume", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("epic", "timestamp", name="uq_candle_epic_timestamp"),
    )
    op.create_index(
        "ix_candle_epic_timestamp", "candle", ["epic", "timestamp"], unique=False
    )


def downgrade() -> None:
    """Drop the candle table."""
    op.drop_index("ix_candle_epic_timestamp", table_name="candle")
    op.drop_table("candle")
