"""In-memory rolling candle buffer — the synchronous read side of the price feed.

Holds the last N **completed 1-minute candles** per epic. It is the hot path: all
decision code (``EntryStrategy.evaluate``, ``CloseProfile.evaluate``, every stop
and zone updater) is synchronous and reads an :class:`EpicBuffer` directly, so
indicators are recomputed for the whole universe on every pass without any I/O.
The durable copy of the same candles lives in the ``candle`` table, which is what
rehydrates this buffer after a restart and backs the day charts.

Two properties of this module are load-bearing and easy to break — see
``docs/DATAFLOW.md``:

- ``max_candles`` is a **hard ceiling on the history any strategy can see**,
  whatever lookback that strategy declares (configured by ``BUFFER_MAX_CANDLES``);
- :meth:`PriceBuffer.append_candles` deduplicates on a **strictly increasing**
  timestamp, which is only safe because the feed filters out partial candles.

Data is ephemeral: lost on restart, cleared by the daily reset.
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

    @property
    def max_candles(self) -> int:
        """Per-epic capacity — the ceiling on every strategy's usable lookback.

        Exposed so the orchestration layer can check a strategy's declared warm-up
        against what the buffer can actually hold, instead of letting it run on a
        silently truncated window (see ``docs/DATAFLOW.md`` §3).
        """
        return self._max_candles

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
        """Append candles newer than the buffer's newest, in order.

        Deduplication is a **strictly increasing** timestamp test, which
        deduplicates the overlapping windows of repeated ``/prices`` fetches.

        This is only correct because callers pass **consolidated** candles: every
        Lightstreamer frame of a given minute carries the same ``UTM``, so feeding
        partial frames here would keep the FIRST sample of each minute and silently
        drop the finished candle that follows it — no error, just a history quietly
        degraded to start-of-minute prices. The ``CONS_END == "1"`` filter in
        :mod:`src.feed.streaming` is what upholds that, and it must stay.
        """
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
