"""Position model — ported from Position.php."""

import enum
from datetime import date, time
from decimal import Decimal

from sqlalchemy import Date, Enum, Integer, Numeric, String, Time
from sqlalchemy.orm import Mapped, mapped_column

from src.models.database import Base


class PositionState(str, enum.Enum):
    """Position state."""

    OPEN = "open"
    CLOSE = "close"


class PositionStrategy(str, enum.Enum):
    """Position closing strategy."""

    TARGET = "target"
    FINISH = "finish"


class Position(Base):
    """Open/closed trading position."""

    __tablename__ = "position"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    epic: Mapped[str] = mapped_column(String(30), nullable=False)
    epic_name: Mapped[str] = mapped_column(String(10), nullable=False)
    deal_reference: Mapped[str | None] = mapped_column(String(30))
    deal_id: Mapped[str | None] = mapped_column(String(50))
    fix_open: Mapped[int | None] = mapped_column(Integer)
    fix_close: Mapped[int | None] = mapped_column(Integer)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    time_open: Mapped[time | None] = mapped_column(Time)
    time_close: Mapped[time | None] = mapped_column(Time)
    time_open_broker: Mapped[time | None] = mapped_column(Time)
    time_close_broker: Mapped[time | None] = mapped_column(Time)
    state: Mapped[PositionState] = mapped_column(
        Enum(PositionState), default=PositionState.OPEN
    )
    strategy: Mapped[PositionStrategy | None] = mapped_column(Enum(PositionStrategy))
    level_follower: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    level_win: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    level_zero: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    level_open: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    level_loose: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    level_security: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    level_close: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    level_stop: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    pip_spread: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    reason_close: Mapped[str | None] = mapped_column(String(30))
    size: Mapped[int | None] = mapped_column(Integer)
    negative: Mapped[int | None] = mapped_column(Integer)
    quantity: Mapped[int | None] = mapped_column(Integer)
    win: Mapped[int | None] = mapped_column(Integer)
    stop_update: Mapped[int | None] = mapped_column(Integer)
    euro: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    euro_stop: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    euro_max: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    euro_min: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
