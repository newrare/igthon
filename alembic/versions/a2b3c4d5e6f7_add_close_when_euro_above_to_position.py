"""add close_when_euro_above to position

Revision ID: a2b3c4d5e6f7
Revises: e7f8a9b0c1d2
Create Date: 2026-09-08

Armed manual exit set from the dashboard chart: the euro P&L threshold above
which the position must be closed on the next monitor tick. Persisted (rather
than kept in memory) so the order survives a restart, exactly like the manual
stop override it sits next to. NULL = nothing armed.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a2b3c4d5e6f7"
down_revision: str | None = "e7f8a9b0c1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "position",
        sa.Column("close_when_euro_above", sa.Numeric(10, 3), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("position", "close_when_euro_above")
