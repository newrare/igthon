"""Candle model — persisted price history for charting and backtesting.

Unlike the in-memory ``PriceBuffer`` (which holds only the last 200 candles per
epic and is lost on restart), this table durably stores every candle collected
during ``collect_and_analyze``. It is the data source for the trade-curve charts
and, once dumped to disk, for offline simulations.

No extra IG API calls are made to populate it: candles are tapped from the same
fetch that feeds the buffer.
"""

from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Float,
    Index,
    Integer,
    String,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.models.database import Base


class UTCDateTime(TypeDecorator):
    """Timezone-aware datetime that always round-trips as UTC.

    SQLite has no native timezone support and returns naive datetimes even when
    the column is declared ``timezone=True``. This decorator normalises values to
    UTC on the way in and re-attaches ``UTC`` on the way out, so comparisons and
    display logic never mix naive and aware datetimes — on SQLite *and* Postgres.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):  # noqa: ANN001
        if value is not None and value.tzinfo is not None:
            return value.astimezone(UTC)
        return value

    def process_result_value(self, value: datetime | None, dialect):  # noqa: ANN001
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class CandleRecord(Base):
    """A single persisted price candle for an epic.

    Stores the full OHLC (bid + offer) so simulations can rebuild the exact
    candle the live strategy saw. Deduplicated on ``(epic, timestamp)``.
    """

    __tablename__ = "candle"
    __table_args__ = (
        UniqueConstraint("epic", "timestamp", name="uq_candle_epic_timestamp"),
        Index("ix_candle_epic_timestamp", "epic", "timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    epic: Mapped[str] = mapped_column(String(30), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    bid_open: Mapped[float] = mapped_column(Float, nullable=False)
    bid_close: Mapped[float] = mapped_column(Float, nullable=False)
    bid_high: Mapped[float] = mapped_column(Float, nullable=False)
    bid_low: Mapped[float] = mapped_column(Float, nullable=False)
    offer_open: Mapped[float] = mapped_column(Float, nullable=False)
    offer_close: Mapped[float] = mapped_column(Float, nullable=False)
    offer_high: Mapped[float] = mapped_column(Float, nullable=False)
    offer_low: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[int] = mapped_column(Integer, default=0)
