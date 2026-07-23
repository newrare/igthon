"""Position model — ported from Position.php."""

import enum
from datetime import date, time
from decimal import Decimal

from sqlalchemy import JSON, Date, Enum, Integer, Numeric, String, Time
from sqlalchemy.orm import Mapped, mapped_column

from src.models.database import Base


class PositionState(enum.StrEnum):
    """Position state."""

    OPEN = "open"
    CLOSE = "close"


class PositionStrategy(enum.StrEnum):
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
    date: Mapped[date] = mapped_column(Date, nullable=False)
    time_open: Mapped[time | None] = mapped_column(Time)
    time_close: Mapped[time | None] = mapped_column(Time)
    time_open_broker: Mapped[time | None] = mapped_column(Time)
    time_close_broker: Mapped[time | None] = mapped_column(Time)
    state: Mapped[PositionState] = mapped_column(
        Enum(PositionState), default=PositionState.OPEN
    )
    # Trade side: "BUY" (long) or "SELL" (short). The live pipeline is long-only,
    # so this defaults to BUY and every normal open leaves it BUY. The loss-recovery
    # feature (src/execution/recovery.py) is the only path that opens a SELL, managed
    # by a mirrored short exit (src/exit/recovery_short.py). Direction-aware P&L and
    # close order side key off this column. See migration a1c2e3f4d5b6.
    direction: Mapped[str] = mapped_column(String(4), nullable=False, default="BUY")
    strategy: Mapped[PositionStrategy | None] = mapped_column(Enum(PositionStrategy))
    # Price levels are stored with 5 decimals: forex pairs (e.g. GBP/EUR at
    # 1.15729) move below the 3rd decimal, so Numeric(10, 3) silently truncated
    # the entire price move to zero. See migration a7b8c9d0e1f2.
    level_follower: Mapped[Decimal | None] = mapped_column(Numeric(12, 5))
    level_win: Mapped[Decimal | None] = mapped_column(Numeric(12, 5))
    level_zero: Mapped[Decimal | None] = mapped_column(Numeric(12, 5))
    level_open: Mapped[Decimal | None] = mapped_column(Numeric(12, 5))
    level_loose: Mapped[Decimal | None] = mapped_column(Numeric(12, 5))
    level_security: Mapped[Decimal | None] = mapped_column(Numeric(12, 5))
    level_close: Mapped[Decimal | None] = mapped_column(Numeric(12, 5))
    level_stop: Mapped[Decimal | None] = mapped_column(Numeric(12, 5))
    # Margin level frozen at open (break-even + noise margin). The trailing stop
    # is never parked between break-even and this level. See migration
    # b3c4d5e6f7a8.
    level_margin: Mapped[Decimal | None] = mapped_column(Numeric(12, 5))
    # IG's minimum stop distance (price units, already padded by
    # ``strategy_stop_min_distance_margin``) captured from the dealing rules at
    # open. Bounds every later broker-stop ratchet so the level pushed to IG
    # never sits inside IG's minimum-distance floor — a stop posted too close to
    # the market is rejected ("Stop trop près") and the previous, far broker
    # order silently stays live. NULL for adopted/legacy rows opened without this
    # data: the ratchet then applies no clamp. See migration d3e4f5a6b7c8.
    min_stop_distance: Mapped[Decimal | None] = mapped_column(Numeric(12, 5))
    pip_spread: Mapped[Decimal | None] = mapped_column(Numeric(12, 5))
    reason_open: Mapped[str | None] = mapped_column(String(30))
    reason_close: Mapped[str | None] = mapped_column(String(30))
    # Name of the close profile that manages this position's exit for its whole
    # life (e.g. "close_zoneprofit"). Set at open by the execution layer from
    # the selected CloseProfile; decoupled from the entry strategy. Adopted/legacy
    # rows may carry a removed profile name, resolved via the legacy aliases in
    # src/exit/__init__.py. See src/exit/ and migration d5e6f7a8b9c0.
    close_profile: Mapped[str | None] = mapped_column(String(30))
    size: Mapped[int | None] = mapped_column(Integer)
    quantity: Mapped[int | None] = mapped_column(Integer)
    win: Mapped[int | None] = mapped_column(Integer)
    stop_update: Mapped[int | None] = mapped_column(Integer)
    # Timestamped trajectory of the protective stop: the initial level set at
    # open plus one entry per ratchet update. Each element is
    # ``{"t": "<UTC ISO8601>", "level": <float>}``. Lets the chart draw the
    # stop's real stepped path instead of a single flat line at the frozen
    # initial level (which never matched the live trailing stop at exit). See
    # migration f7a8b9c0d1e2.
    stop_history: Mapped[list | None] = mapped_column(JSON)
    # Zone (see src/exit/zones) the live bid sat in when the user manually raised
    # the stop from the dashboard chart buttons. While set, automatic per-zone
    # ratcheting is suspended and the user-placed stop is held; it is cleared once
    # the bid crosses into a different zone (auto management then resumes). NULL =
    # no manual override active. See migration c2d3e4f5a6b7.
    manual_stop_zone: Mapped[str | None] = mapped_column(String(20))
    euro: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    euro_stop: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    # Euros of realized/unrealized P&L per 1.0 of price movement for the whole
    # position: P&L = (close - open) * euro_per_point. Derived at open from IG
    # market data (contract size x quote->EUR exchange rate x deal size), so it
    # already accounts for currency conversion (e.g. JPY pairs) and the real
    # per-point value of the instrument. See migration a7b8c9d0e1f2.
    euro_per_point: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    euro_max: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    euro_min: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
