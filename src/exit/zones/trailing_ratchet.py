"""Zone 3 updater — the bid has cleared the margin level (real profit).

This is the trailing that works well in live trading and is moved here
**verbatim** from the old ``atr_trailing_profit`` per-tick logic:

- **momentum confirmation** — only ratchet when the last two recorded bids are
  both rising (``bid[-3] < bid[-2] < bid[-1]``). A lone upward spike (one up-step)
  is ignored, so the stop is not tightened on a one-tick peak that falls back;
- **ATR chandelier ratchet** — the stop trails ``k × ATR`` below price and only
  ever moves up (see :func:`~src.exit.trailing.compute_trailing_stop`). The
  initial-risk ceiling is deliberately **disabled** (``euro_stop=0``): this
  updater only ever runs once the trade is positive beyond the noise margin, so
  it protects *acquired gain*, not the risk accepted at open. Keeping the ceiling
  would pin the gap to the initial risk distance and make this updater's dedicated
  trailing width a no-op; the ``2 × spread`` anti-noise floor still applies;
- **anti-band guard** — the new stop is never parked at or below the (open-frozen)
  margin level; a stop there would be triggered by noise alone for ~zero profit.

The result is a stop that climbs in steps, staying ``k × ATR`` below the running
high.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.exit.trailing import compute_trailing_stop
from src.exit.zones.base import StopContext, StopUpdater
from src.feed.price_buffer import EpicBuffer


@dataclass
class TrailingRatchetStop(StopUpdater):
    """Momentum-gated ATR chandelier that trails the bid up in steps."""

    name = "trailing_ratchet"

    # Dedicated trailing width (× ATR). Kept equal pre/post break-even: tightening
    # after break-even cuts winners short on a trend-following breakout.
    atr_k_pre: float = 2.5
    atr_k_post: float = 2.5
    trailing_step_ratio: float = 0.3  # min advance (× ATR) before re-pushing stop

    @staticmethod
    def _last_two_bids_rising(buf: EpicBuffer) -> bool:
        """True when the last two recorded bid moves are both upward.

        Requires ``bid[-3] < bid[-2] < bid[-1]`` (at least three recorded bids). A
        single rising spike yields only one up-step and so fails this check — that
        is exactly the one-tick peak we refuse to ratchet on.
        """
        closes = buf.bid_closes
        if len(closes) < 3:
            return False
        return closes[-3] < closes[-2] < closes[-1]

    def propose(self, ctx: StopContext) -> float | None:
        # Momentum confirmation: only ratchet when the last two recorded bids are
        # both rising. A lone upward spike (one up-step) is ignored so the stop is
        # not tightened on a one-tick peak that falls back right after.
        if not self._last_two_bids_rising(ctx.buf):
            return None

        # Standard ATR chandelier ratchet (up only). ``euro_stop=0`` deliberately
        # disables the initial-risk ceiling: this updater only runs once the trade
        # is positive beyond the noise margin, so it protects acquired gain rather
        # than the risk accepted at open. The ``2 × spread`` anti-noise floor still
        # applies inside ``compute_trailing_stop``.
        new_stop = compute_trailing_stop(
            ctx.current_bid,
            atr_value=ctx.atr_value,
            spread=ctx.spread,
            level_zero=ctx.level_zero,
            level_follower=ctx.level_follower,
            euro_per_point=ctx.euro_per_point,
            euro_stop=0.0,
            config=self,
        )
        if new_stop is None:
            return None

        # Never park the stop in the dead band between break-even and the margin
        # level: a stop there would be triggered by noise alone for ~zero profit.
        if new_stop <= ctx.level_margin:
            return None

        return new_stop
