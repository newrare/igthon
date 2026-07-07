"""drop unused position columns (fix_open, fix_close, negative)

Revision ID: b1c2d3e4f5a6
Revises: a1c2e3f4d5b6
Create Date: 2026-07-06

These three integer columns were carried over from the PHP port and are never
read or written by the application (verified: no ORM attribute access anywhere).
Dropping them removes dead schema. All three are nullable with no data the app
relies on; the downgrade re-adds them (empty).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "a1c2e3f4d5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("position", "fix_open")
    op.drop_column("position", "fix_close")
    op.drop_column("position", "negative")


def downgrade() -> None:
    op.add_column("position", sa.Column("negative", sa.Integer(), nullable=True))
    op.add_column("position", sa.Column("fix_close", sa.Integer(), nullable=True))
    op.add_column("position", sa.Column("fix_open", sa.Integer(), nullable=True))
