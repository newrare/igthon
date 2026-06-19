"""In-memory rolling price buffer for real-time analysis.

Replaces the old HistoryDay DB table. Keeps the last N candles per epic
in memory — sufficient for indicator calculations (SMA, regression, ROC).
Data is ephemeral and purged at end of day.
"""

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

# Default number of candles to keep per epic
DEFAULT_MAX_CANDLES = 200


@dataclass(slots=True)
class Candle:
    """Single price candle from the IG API."""

    timestamp: datetime
    bid_open: float
    bid_close: float
    bid_high: float
    bid_low: float
    offer_open: float
    offer_close: float
    offer_high: float
    offer_low: float
    volume: int = 0

    @property
    def mid_close(self) -> float:
        """Mid-price at close."""
        return (self.bid_close + self.offer_close) / 2

    @property
    def spread(self) -> float:
        """Current spread (offer - bid)."""
        return self.offer_close - self.bid_close


@dataclass
class EpicBuffer:
    """Rolling buffer for a single epic."""

    epic: str
    max_candles: int = DEFAULT_MAX_CANDLES
    candles: deque[Candle] = field(default_factory=deque)

    def __post_init__(self) -> None:
        self.candles = deque(maxlen=self.max_candles)

    def add(self, candle: Candle) -> None:
        """Add a candle to the buffer."""
        self.candles.append(candle)

    @property
    def last(self) -> Candle | None:
        """Most recent candle."""
        return self.candles[-1] if self.candles else None

    @property
    def bid_closes(self) -> list[float]:
        """List of bid close prices (oldest to newest)."""
        return [c.bid_close for c in self.candles]

    @property
    def offer_closes(self) -> list[float]:
        """List of offer close prices (oldest to newest)."""
        return [c.offer_close for c in self.candles]

    @property
    def mid_closes(self) -> list[float]:
        """List of mid close prices (oldest to newest)."""
        return [c.mid_close for c in self.candles]

    @property
    def spreads(self) -> list[float]:
        """List of spread values (oldest to newest)."""
        return [c.spread for c in self.candles]

    def __len__(self) -> int:
        return len(self.candles)


class PriceBuffer:
    """Central price buffer managing all tracked epics.

    Usage:
        buffer = PriceBuffer(max_candles=100)
        buffer.update("IX.D.DAX.IFMM.IP", candles)
        prices = buffer.get("IX.D.DAX.IFMM.IP").bid_closes
    """

    def __init__(self, max_candles: int = DEFAULT_MAX_CANDLES) -> None:
        self._max_candles = max_candles
        self._buffers: dict[str, EpicBuffer] = {}

    def get(self, epic: str) -> EpicBuffer | None:
        """Get the buffer for an epic, or None if not tracked."""
        return self._buffers.get(epic)

    def get_or_create(self, epic: str) -> EpicBuffer:
        """Get or create the buffer for an epic."""
        if epic not in self._buffers:
            self._buffers[epic] = EpicBuffer(epic=epic, max_candles=self._max_candles)
        return self._buffers[epic]

    def update(self, epic: str, candles: list[Candle]) -> None:
        """Replace the buffer content for an epic with fresh candles."""
        buf = self.get_or_create(epic)
        buf.candles.clear()
        for candle in candles:
            buf.add(candle)
        logger.debug("Buffer updated: %s (%d candles)", epic, len(buf))

    def append_candles(self, epic: str, candles: list[Candle]) -> None:
        """Append candles to the buffer, skipping those already present
        (matched by timestamp)."""
        buf = self.get_or_create(epic)
        last_ts = buf.last.timestamp if buf.last else None
        added = 0
        for candle in candles:
            if last_ts is None or candle.timestamp > last_ts:
                buf.add(candle)
                last_ts = candle.timestamp
                added += 1
        if added:
            logger.debug(
                "Buffer appended: %s (+%d candles, total %d)", epic, added, len(buf)
            )

    def add_candle(self, epic: str, candle: Candle) -> None:
        """Append a single candle to an epic's buffer."""
        buf = self.get_or_create(epic)
        buf.add(candle)

    def clear(self) -> None:
        """Clear all buffers (end of day reset)."""
        self._buffers.clear()
        logger.info("Price buffer cleared.")

    def clear_epic(self, epic: str) -> None:
        """Clear buffer for a specific epic."""
        if epic in self._buffers:
            del self._buffers[epic]

    @property
    def tracked_epics(self) -> list[str]:
        """List of currently tracked epics."""
        return list(self._buffers.keys())

    def __len__(self) -> int:
        return len(self._buffers)
