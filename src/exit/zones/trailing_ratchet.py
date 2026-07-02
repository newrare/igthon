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

from src.core.indicators import adverse_tick_noise
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

    # Adverse-noise floor on the trailing distance. The candle ATR does not
    # capture the bid's tick-to-tick jitter and shrinks in a clean trend, so the
    # stop can end up hugging the bid and be knocked out by an ordinary pull-back
    # while the trade is still running (observed live on IX.D.StoxxBank.FNI2.IP).
    # The stop is held at least ``noise_mult ×`` the adverse tick-noise band
    # below the bid — measured per-tick, per-epic — so normal noise cannot reach
    # it. See :func:`~src.core.indicators.adverse_tick_noise`.
    noise_window: int = 20  # steps measured for the adverse-noise band
    noise_std_k: float = 2.0  # σ band added to the mean down-move
    noise_mult: float = 2.0  # multiple of that band kept between bid and stop

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
        #
        # The adverse tick-noise band (measured on the live bid) sets a per-tick
        # floor on the trailing distance so the stop is never parked closer to
        # the bid than an ordinary pull-back — the fix for winners stopped out on
        # noise in a clean trend where the candle ATR has shrunk.
        noise_floor = self.noise_mult * adverse_tick_noise(
            ctx.buf.bid_closes, self.noise_window, self.noise_std_k
        )
        new_stop = compute_trailing_stop(
            ctx.current_bid,
            atr_value=ctx.atr_value,
            spread=ctx.spread,
            level_zero=ctx.level_zero,
            level_follower=ctx.level_follower,
            euro_per_point=ctx.euro_per_point,
            euro_stop=0.0,
            config=self,
            noise_floor=noise_floor,
        )
        if new_stop is None:
            return None

        # Never park the stop in the dead band between break-even and the margin
        # level: a stop there would be triggered by noise alone for ~zero profit.
        if new_stop <= ctx.level_margin:
            return None

        return new_stop
