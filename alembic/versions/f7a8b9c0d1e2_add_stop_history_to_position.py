"""add stop_history to position

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-06-29

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f7a8b9c0d1e2"
down_revision: str | None = "e6f7a8b9c0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Timestamped trajectory of the protective stop (initial level + each
    # ratchet update) so the chart can draw the stop's real stepped path. JSON
    # maps to JSONB on PostgreSQL; nullable for rows predating the capture.
    op.add_column(
        "position",
        sa.Column("stop_history", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("position", "stop_history")
