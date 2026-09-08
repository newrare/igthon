"""Add market_catalog and catalog_node tables (static IG catalog + resumable crawl)

Revision ID: e7f8a9b0c1d2
Revises: d3e4f5a6b7c8
Create Date: 2026-09-08 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7f8a9b0c1d2"
down_revision: str | None = "d3e4f5a6b7c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the static market catalog and its crawl frontier."""
    op.create_table(
        "market_catalog",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("epic", sa.String(length=60), nullable=False),
        sa.Column("instrument_name", sa.String(length=120), nullable=True),
        sa.Column("instrument_type", sa.String(length=40), nullable=True),
        sa.Column("expiry", sa.String(length=30), nullable=True),
        sa.Column("node_id", sa.String(length=40), nullable=True),
        sa.Column("node_path", sa.String(length=255), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("epic", name="uq_market_catalog_epic"),
    )
    op.create_index(
        "ix_market_catalog_instrument_type",
        "market_catalog",
        ["instrument_type"],
        unique=False,
    )
    op.create_index(
        "ix_market_catalog_node_id", "market_catalog", ["node_id"], unique=False
    )

    op.create_table(
        "catalog_node",
        sa.Column("node_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=True),
        sa.Column("parent_id", sa.String(length=40), nullable=True),
        sa.Column("path", sa.String(length=255), nullable=True),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("markets_found", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=200), nullable=True),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("visited_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("node_id"),
    )
    op.create_index("ix_catalog_node_status", "catalog_node", ["status"], unique=False)


def downgrade() -> None:
    """Drop the market catalog tables."""
    op.drop_index("ix_catalog_node_status", table_name="catalog_node")
    op.drop_table("catalog_node")
    op.drop_index("ix_market_catalog_node_id", table_name="market_catalog")
    op.drop_index("ix_market_catalog_instrument_type", table_name="market_catalog")
    op.drop_table("market_catalog")
