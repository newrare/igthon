"""add manual_stop_zone to position

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-07-21

Records the price zone a manually-raised stop was placed in (from the dashboard
chart buttons). While set, the automatic per-zone ratcheting is suspended and the
user-placed stop is held; it is cleared once the live bid crosses into a
different zone, at which point automatic management resumes.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c2d3e4f5a6b7"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "position",
        sa.Column("manual_stop_zone", sa.String(20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("position", "manual_stop_zone")
