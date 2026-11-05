"""Far initial stop — a catastrophe stop sized on quarter-hour bars.

Places the initial protective stop ``stop_atr_k × ATR`` away from the entry,
the ATR being measured on **15-minute bars**
(:func:`~src.core.indicators.estimated_timeframe_atr`) rather than on the one-minute
candles :class:`~src.stops.stop_atr.StopAtr` reads.
At 8 quarter-hour ATRs the stop sits well outside a day's ordinary swing: it is
not meant to manage the trade, only to cap a market that runs away, while the
close profile (the ``basket`` group close) decides the exit.

Measured with the daily-basket replay (2026-10-23, W24–W42): at 8 ATR15 the
stop fired on 8 of 162 legs; a wider stop or none made every exit rule worse.
The quantity is always IG's minimum deal size (it does not shrink with the stop
distance), so a far stop multiplies the euro risk carried by each position —
``Position.euro_stop`` records it at open.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.indicators import estimated_timeframe_atr
from src.feed.price_buffer import EpicBuffer
from src.stops.base import StopDistance

#: Bar length the distance is measured on, in minutes. Shared with
#: :class:`~src.entry.open_basket.OpenBasket` and the ``basket`` close so the
#: entry's spread gate, the stop and the basket target speak the same unit.
FAR_STOP_BAR_MINUTES = 15
#: Distance of the stop, in quarter-hour ATRs. The ``basket`` close derives its
#: one-ATR target from this figure: keep the two in step.
FAR_STOP_ATR_K = 8.0


@dataclass
class StopFar(StopDistance):
    """Initial stop ``stop_atr_k`` quarter-hour ATRs from the entry."""

    name = "stop_far"

    bar_minutes: int = FAR_STOP_BAR_MINUTES
    atr_period: int = 14
    stop_atr_k: float = FAR_STOP_ATR_K

    @classmethod
    def from_settings(cls, settings) -> StopFar:
        # Parameters are constants of this class (the field defaults above), so
        # the policy builds from those and ignores ``settings``.
        return cls()

    def initial_stop(
        self,
        *,
        entry_level: float,
        direction: str,
        buf: EpicBuffer,
        day_extreme: float | None = None,  # unused: the window fits in the buffer
    ) -> float:
        last = buf.last
        # Same estimate as ``open_basket`` reads, so a short session still gets
        # a real distance — a zero ATR would park the stop on the entry.
        atr_value = estimated_timeframe_atr(
            list(buf.candles), self.bar_minutes, self.atr_period
        )
        distance = self.stop_atr_k * atr_value
        if direction == "SELL":
            offer = last.offer_close if last else entry_level
            return offer + distance
        return entry_level - distance
