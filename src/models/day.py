"""Day model — ported from Day.php."""

import enum
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, Enum, Integer, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models.database import Base


class DayState(str, enum.Enum):
    """Day trading state."""

    OPEN = "open"
    CLOSE = "close"


class Day(Base):
    """Daily trading summary."""

    __tablename__ = "day"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    state: Mapped[DayState] = mapped_column(Enum(DayState), default=DayState.OPEN)
    euro_list: Mapped[str | None] = mapped_column(Text)
    euro_total: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
