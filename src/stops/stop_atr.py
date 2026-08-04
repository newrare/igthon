"""Flat ATR initial stop — the reference distance policy.

Places the initial protective stop a flat ``stop_atr_k × ATR(period)`` away from
the entry (below for a BUY, above for a SELL). This reproduces the placement that
used to live in the old ``atr_trailing`` close profile, now expressed as a
standalone, swappable :class:`~src.stops.base.StopDistance`. It is the simple
baseline against which :class:`~src.stops.stop_support.StopSupport` is
compared.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.indicators import atr
from src.feed.price_buffer import EpicBuffer
from src.stops.base import StopDistance


@dataclass
class StopAtr(StopDistance):
    """Initial stop a flat ``stop_atr_k × ATR`` from the entry."""

    name = "stop_atr"

    atr_period: int = 14
    stop_atr_k: float = 2.5  # initial protective stop distance, in ATR multiples

    @classmethod
    def from_settings(cls, settings) -> StopAtr:
        # Parameters are constants of this class (the field defaults above), so
        # the policy builds from those and ignores ``settings``.
        return cls()

    def initial_stop(
        self,
        *,
        entry_level: float,
        direction: str,
        buf: EpicBuffer,
        day_extreme: float | None = None,  # unused: this window fits in the buffer
    ) -> float:
        last = buf.last
        atr_value = atr(list(buf.candles), self.atr_period)
        distance = self.stop_atr_k * atr_value
        if direction == "SELL":
            offer = last.offer_close if last else entry_level
            return offer + distance
        return entry_level - distance
