"""SelectionPreference model — persists the dashboard-chosen entry/close.

Stores the active entry strategy and close profile chosen at runtime from the
dashboard so the choice survives a restart. Replaces the role of
``ENTRY_STRATEGY_NAME`` / ``CLOSE_PROFILE_NAME`` in ``.env``, which now serve
only as the initial fallback when nothing has been persisted yet.
"""

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models.database import Base


class SelectionPreference(Base):
    """Last dashboard-chosen entry strategy / close profile, restored on startup.

    Keyed by ``kind`` — ``"entry"`` or ``"close"`` — with ``name`` holding the
    registry key (the ``ENTRY_STRATEGY_NAME`` / ``CLOSE_PROFILE_NAME`` value).
    """

    __tablename__ = "selection_preference"

    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
