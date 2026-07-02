"""drop selection_preference table

The open / stop / close selection now comes exclusively from ``.env`` (the single
source of truth); it is no longer switchable from the dashboard nor persisted, so
the ``selection_preference`` table is obsolete.

Revision ID: a9b8c7d6e5f4
Revises: f7a8b9c0d1e2
Create Date: 2026-07-02

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a9b8c7d6e5f4"
down_revision: str | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("selection_preference")


def downgrade() -> None:
    op.create_table(
        "selection_preference",
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
