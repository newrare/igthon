"""Epic model — ported from Epic.php."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models.database import Base


class Epic(Base):
    """Instrument (epic) information."""

    __tablename__ = "epic"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(30), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(String(100))
    type: Mapped[str | None] = mapped_column(String(30))
    deposit: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    # Timestamp of the last navigation-tree crawl that included this epic.
    # Persists the daily epic list across restarts (see BotScheduler).
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Whether this epic is in the current tradable subset (open + TRADEABLE filter).
    # Updated hourly by _refresh_tradable_epics; restored on startup by load_persisted_state.
    is_tradable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
