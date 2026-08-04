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

:class:`TrailingRatchetMoreStop` (``trailing_ratchetmore``) is the same skeleton
with one added conviction: **the further a run goes, the less of it should be given
back**. See its own docstring.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

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

    def _trailing_config(self, ctx: StopContext) -> TrailingRatchetStop:
        """The width configuration handed to the chandelier on this tick.

        A hook, not a decision: this updater trails at one constant width, so it
        hands over itself. :class:`TrailingRatchetMoreStop` overrides it to narrow
        the width as the acquired gain grows.
        """
        return self

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
                config=self._trailing_config(ctx),
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


@dataclass
class TrailingRatchetMoreStop(TrailingRatchetStop):
    """``trailing_ratchet`` that keeps a growing share of the run it just made.

    Same three candidates as its parent (support-anchored floor, momentum-gated
    chandelier, tighten-only), plus one conviction: **the further a run goes, the
    less of it should be handed back**. The parent trails an extended winner at the
    exact width it started with and, when price turns hard, its sharp-reversal guard
    holds the stop where it was — so a trade that ran five ATR into profit can walk
    all the way back to a stop set when the run began. Two additions close that:

    * **give-back cap** — the stop is never left further back than
      ``giveback_retention`` of the *best excursion the position ever reached*, once
      that peak is worth at least ``giveback_arm_atr × ATR``. Its anchor is the peak
      itself, which is what makes it different in kind from the other two
      candidates: the chandelier reads the live price and is momentum-gated (idle
      exactly when price is falling), and the lock floor reads a swing low that lags
      by its confirmation window. The cap therefore keeps working during the
      reversal — the moment the gain is actually at risk. It is deliberately *not*
      subject to the sharp-reversal hold, since a peak-anchored level cannot be
      pushed forward by a stale anchor.
    * **progressive width** — the chandelier's ``k × ATR`` narrows from
      ``atr_k_post`` towards ``atr_k_floor``, by ``atr_k_shrink_per_atr`` per ATR of
      peak gain beyond the arming threshold. Early on the trade keeps the full width
      it needs to breathe; deep in a run, where the parent still leaves 2.5 ATR of
      air, the stop follows closer.

    Both are bounded by the parent's own guards: nothing lands in the dead band
    between break-even and the margin, nothing sits at or past the live price, and
    the stop is never loosened. Setting ``giveback_retention=0`` and
    ``atr_k_shrink_per_atr=0`` reduces this updater to ``trailing_ratchet``
    exactly, which is what the tests use to isolate each mechanism.
    """

    name = "trailing_ratchetmore"

    #: Share of the peak excursion (measured from break-even) the stop keeps
    #: locked. ``0.5`` = at most half the best gain is ever given back; ``0``
    #: disables the cap.
    giveback_retention: float = 0.5
    #: Peak gain, in ATR, required before the cap and the narrowing arm. Below it
    #: the trade has no run worth protecting and the parent's behaviour is kept.
    giveback_arm_atr: float = 1.0
    #: Narrowest trailing width the progressive shrink may reach (× ATR).
    atr_k_floor: float = 1.2
    #: Width removed per ATR of peak gain beyond ``giveback_arm_atr``. ``0``
    #: disables the narrowing.
    atr_k_shrink_per_atr: float = 0.25

    def _peak_gain(self, ctx: StopContext) -> float:
        """Best profit distance past break-even reached since the open, in points.

        Read from the recorded close-out prices, which the close profile bounds to
        this position's own open — so "the peak" is this trade's peak, not an
        earlier intraday one. Never negative: a trade that has not been in profit
        has no run to protect.
        """
        closes = ctx.favourable_closes
        if not closes:
            return 0.0
        return max(0.0, max(closes) - ctx.favourable(ctx.level_zero))

    def _armed(self, ctx: StopContext) -> float:
        """Peak gain once it is worth acting on, ``0`` while it is not.

        Both additions share the same arming test, so they switch on together: a
        run has to be at least ``giveback_arm_atr × ATR`` deep before this updater
        starts behaving differently from its parent.
        """
        if ctx.atr_value <= 0:
            return 0.0
        peak = self._peak_gain(ctx)
        return peak if peak >= self.giveback_arm_atr * ctx.atr_value else 0.0

    def _trailing_config(self, ctx: StopContext) -> TrailingRatchetStop:
        """Narrow the chandelier width in proportion to the run made so far."""
        peak = self._armed(ctx)
        if peak <= 0 or self.atr_k_shrink_per_atr <= 0:
            return self
        excess_atr = peak / ctx.atr_value - self.giveback_arm_atr
        k = max(
            self.atr_k_floor, self.atr_k_post - self.atr_k_shrink_per_atr * excess_atr
        )
        return replace(self, atr_k_pre=k, atr_k_post=k)

    def _giveback_level(self, ctx: StopContext) -> float | None:
        """Stop level that concedes at most ``1 − retention`` of the peak gain.

        Returns ``None`` when the cap is disabled, the run is not yet deep enough,
        or the level cannot be placed safely: at or past the live price (the
        software backstop would close the position on the spot) or short of the
        margin (the parent's dead-band rule — a level there is reachable by noise
        alone for ~zero profit, and unlike the support-anchored floor this one has
        no swing low under it).
        """
        if self.giveback_retention <= 0:
            return None
        peak = self._armed(ctx)
        if peak <= 0:
            return None
        level = ctx.offset(ctx.level_zero, self.giveback_retention * peak)
        if not ctx.beyond(ctx.current_price, level):
            return None
        if not ctx.beyond(level, ctx.level_margin):
            return None
        return level

    def propose(self, ctx: StopContext) -> float | None:
        # The parent decides the floor/chandelier pair (and holds on a sharp
        # reversal); the give-back cap is evaluated on every tick, reversal
        # included, since that is when it earns its keep.
        candidates = [
            candidate
            for candidate in (super().propose(ctx), self._giveback_level(ctx))
            if candidate is not None
        ]
        if not candidates:
            return None
        target = max(candidates, key=ctx.gain)

        # Tighten-only, re-checked here: the cap bypasses the parent's own check.
        if ctx.level_follower > 0 and not ctx.beyond(target, ctx.level_follower):
            return None
        return target
