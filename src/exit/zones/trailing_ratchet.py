"""Zone 3 updater — price has cleared the profit trigger (real profit).

This is the trailing that works well in live trading and is moved here
**verbatim** from the old ``atr_trailing_profit`` per-tick logic:

- **momentum confirmation** — only ratchet when the last two recorded ticks both
  moved into profit (``p[-3] < p[-2] < p[-1]`` in sign-normalised terms). A lone
  favourable spike (one step) is ignored, so the stop is not tightened on a
  one-tick peak that falls back;
- **ATR chandelier ratchet** — the stop trails ``k × ATR`` behind price and only
  ever moves towards profit (see
  :func:`~src.exit.trailing.compute_trailing_stop`). The initial-risk ceiling is
  deliberately **disabled** (``euro_stop=0``): this updater only ever runs once the
  trade is positive beyond the noise margin, so it protects *acquired gain*, not
  the risk accepted at open. Keeping the ceiling would pin the gap to the initial
  risk distance and make this updater's dedicated trailing width a no-op; the
  ``2 × spread`` anti-noise floor still applies;
- **anti-band guard** — the new stop is never parked short of the (open-frozen)
  margin level; a stop there would be triggered by noise alone for ~zero profit.

The result is a stop that climbs in steps, staying ``k × ATR`` behind the running
extreme — below the running high for a BUY, above the running low for a SELL.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.exit.trailing import compute_trailing_stop
from src.exit.zones.base import (
    BreakevenLockParams,
    StopContext,
    StopUpdater,
    breakeven_lock_level,
)


@dataclass
class TrailingRatchetStop(StopUpdater):
    """Momentum-gated ATR chandelier that trails price in steps, either side."""

    name = "trailing_ratchet"

    # Dedicated trailing width (× ATR). Kept equal pre/post break-even: tightening
    # after break-even cuts winners short on a trend-following breakout.
    atr_k_pre: float = 2.5
    atr_k_post: float = 2.5
    trailing_step_ratio: float = 0.3  # min advance (× ATR) before re-pushing stop

    # Adverse-noise floor on the trailing distance. The candle ATR does not
    # capture price's tick-to-tick jitter and shrinks in a clean trend, so the
    # stop can end up hugging price and be knocked out by an ordinary pull-back
    # while the trade is still running (observed live on IX.D.StoxxBank.FNI2.IP).
    # The stop is held at least ``noise_mult ×`` the adverse tick-noise band
    # behind price — measured per-tick, per-epic — so normal noise cannot reach
    # it. See :func:`~src.core.indicators.adverse_tick_noise`.
    noise_window: int = 20  # steps measured for the adverse-noise band
    noise_std_k: float = 2.0  # σ band added to the mean adverse move
    noise_mult: float = 2.0  # multiple of that band kept between price and stop

    # Sharp-reversal guard. Even the support-anchored lock floor below — which,
    # unlike the chandelier, is NOT momentum-gated — can still step the stop
    # forward while price is running back against us: its swing-low anchor lags by
    # ``confirm_window`` candles, so an old low rolling out of that window advances
    # the floor even as the trade gives its gain back. When price has given back
    # more than ``drop_guard_k × ATR`` from the best of the last
    # ``drop_guard_window`` recorded closes, hold the stop this tick rather than
    # tighten it into a reversal. The stop is never loosened, so the persisted
    # follower keeps protecting the position.
    drop_guard_window: int = 5  # recent closes scanned for the local profit extreme
    drop_guard_k: float = 2.0  # × ATR give-back from it that blocks a tighten

    #: Shared shaping of the support-anchored break-even lock used as this zone's
    #: FLOOR (see ``breakeven_lock_level``), so a bid that jumps straight past the
    #: margin — skipping the margin-zone updater — is never left on its open stop.
    lock: BreakevenLockParams = field(default_factory=BreakevenLockParams)

    @staticmethod
    def _last_two_ticks_favourable(ctx: StopContext) -> bool:
        """True when the last two recorded moves both went into profit.

        Requires ``p[-3] < p[-2] < p[-1]`` on the sign-normalised closes (at least
        three recorded ticks), i.e. two rising bids for a BUY or two falling offers
        for a SELL. A single favourable spike yields only one step and so fails this
        check — that is exactly the one-tick peak we refuse to ratchet on.
        """
        closes = ctx.favourable_closes
        if len(closes) < 3:
            return False
        return closes[-3] < closes[-2] < closes[-1]

    def _sharp_reversal(self, ctx: StopContext) -> bool:
        """True when price has moved sharply back from its recent profit extreme.

        Measured as the give-back of the live price from the best of the last
        ``drop_guard_window`` recorded closes, expressed in ATR units. At or beyond
        ``drop_guard_k × ATR`` the move is a genuine reversal (not tick jitter), so
        no zone should tighten the stop on this tick.
        """
        if ctx.atr_value <= 0 or self.drop_guard_window < 1:
            return False
        closes = ctx.favourable_closes
        if not closes:
            return False
        best = max(closes[-self.drop_guard_window :])
        give_back = best - ctx.favourable(ctx.current_price)
        return give_back >= self.drop_guard_k * ctx.atr_value

    def propose(self, ctx: StopContext) -> float | None:
        # Sharp-reversal guard: never tighten the stop while price is running back
        # against us. This gates BOTH candidates below — chiefly the lock floor,
        # whose lagging swing-low anchor would otherwise keep advancing the stop as
        # an old low rolls out of its window even though price is reversing hard.
        # Holding (returning None) is safe: the persisted follower is never loosened.
        if self._sharp_reversal(ctx):
            return None

        # FLOOR: the support-anchored break-even lock (same rule as the margin
        # zone). This guarantees that entering profit always establishes a
        # protective stop even when the chandelier below is still suppressed — so a
        # bid that jumps straight past the margin, skipping the margin-zone
        # updater, is never left on its open stop (no unmanaged zone). It is safe
        # here despite sitting below the margin because it is placed under a real
        # swing low, net of noise, not at a fixed offset that noise could reach.
        lock_floor = breakeven_lock_level(ctx, self.lock)

        # The ATR chandelier ratchet, momentum-gated: only ratchet when the last
        # two recorded ticks both moved into profit, so a lone favourable spike
        # does not tighten the stop onto a one-tick peak that falls back.
        chandelier: float | None = None
        if self._last_two_ticks_favourable(ctx):
            # ``euro_stop=0`` deliberately disables the initial-risk ceiling: this
            # updater protects acquired gain, not the risk accepted at open. The
            # ``2 × spread`` anti-noise floor still applies inside
            # ``compute_trailing_stop``; the adverse tick-noise band sets a per-tick
            # floor on the trailing distance so the stop never hugs price closer
            # than an ordinary pull-back (winners stopped out on noise in a clean
            # trend where the candle ATR has shrunk).
            noise_floor = self.noise_mult * ctx.adverse_noise(
                self.noise_window, self.noise_std_k
            )
            new_stop = compute_trailing_stop(
                ctx.current_price,
                atr_value=ctx.atr_value,
                spread=ctx.spread,
                level_zero=ctx.level_zero,
                level_follower=ctx.level_follower,
                euro_per_point=ctx.euro_per_point,
                euro_stop=0.0,
                config=self,
                noise_floor=noise_floor,
                sign=ctx.sign,
            )
            # Never park the *chandelier* in the dead band between break-even and
            # the margin: unlike the noise-anchored floor, ``price − k × ATR`` is not
            # placed behind a real support, so a stop there would be tripped by noise
            # alone for ~zero profit.
            if new_stop is not None and ctx.beyond(new_stop, ctx.level_margin):
                chandelier = new_stop

        # Take the candidate furthest into profit: the chandelier once it has
        # climbed clear of the margin, otherwise the lock floor holds the position
        # until the chandelier overtakes it. This is what removes the gap that left
        # the stop pinned at its open level through a whole favourable excursion.
        candidates = [c for c in (chandelier, lock_floor) if c is not None]
        if not candidates:
            return None
        target = max(candidates, key=ctx.gain)

        # Tighten-only. The composer applies the returned level verbatim, so never
        # returning a level short of the current follower is this updater's own
        # responsibility.
        if ctx.level_follower > 0 and not ctx.beyond(target, ctx.level_follower):
            return None
        return target
