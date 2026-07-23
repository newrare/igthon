"""add min_stop_distance to position

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-07-22

Stores IG's minimum stop distance (price units, padded by
``strategy_stop_min_distance_margin``) captured from the dealing rules at open.
Every later broker-stop ratchet is clamped to this floor so the level pushed to
IG never sits inside the minimum-distance rule — a stop posted too close to the
market is rejected ("Stop trop près") and the previous, far broker order
silently stays live (the frozen broker-stop bug). NULL for adopted/legacy rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d3e4f5a6b7c8"
down_revision: str | None = "c2d3e4f5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "position",
        sa.Column("min_stop_distance", sa.Numeric(12, 5), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("position", "min_stop_distance")
