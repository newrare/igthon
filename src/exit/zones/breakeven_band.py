"""Zone 2 updaters — the bid is in the noise band just above break-even.

The bid sits above break-even (``level_zero``) but has not yet cleared the margin
level (``level_zero + noise_margin``). This is the delicate region: parking the
stop a hair above break-even here is exactly where ordinary bid/offer noise alone
would trigger it for ~zero profit (the "everything exits at 0 €" pathology that a
naive break-even pin caused live).

Two updaters live here, selected by ``CLOSE_ZONEMARGE``:

- :class:`BreakevenBandStop` (``hold``) — leave the initial stop untouched; the
  stop only ever moves once the bid clears the margin level (zone 3);
- :class:`BreakevenLockStop` (``breakeven_lock``) — pull the stop up to a small
  fixed margin above break-even, **but only once the bid has run comfortably clear
  of that lock level** so a normal pull-back cannot immediately knock it out. This
  secures a hair of profit early on a fast reversal without hugging the bid.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.indicators import adverse_tick_noise
from src.exit.zones.base import StopContext, StopUpdater


@dataclass
class BreakevenBandStop(StopUpdater):
    """Hold the stop while the bid is in the noise band above break-even."""

    name = "hold"

    def propose(self, ctx: StopContext) -> float | None:
        return None


@dataclass
class BreakevenLockStop(StopUpdater):
    """Lock the stop a small margin above break-even, gated on a comfortable gap.

    The target stop is a small fixed margin above break-even
    (``level_zero + lock_margin_spreads × spread`` — one spread by default, so the
    stop secures a hair of profit rather than an exact break-even that would exit
    at ~0 € on the offer). The move only fires once the live bid sits at least a
    noise-sized gap **above** that target, so an ordinary pull-back cannot trigger
    it the moment it is placed.

    That gap is the same adverse-tick-noise band the profit trailing uses
    (:func:`~src.core.indicators.adverse_tick_noise`), floored on the spread, so on
    a quiet tape a real move is still required. The result is the behaviour asked
    for on the margin zone: raise the stop **early enough** to cap the loss on a
    fast reversal, but **not so close to the bid** that market noise strangles the
    trade to break-even — the failure mode of the old unconditional break-even pin.

    The stop is a *lock*, not a trailing: it snaps to the fixed margin level and
    stays. Once the bid clears the margin level the position enters zone 3 and the
    profit trailing (:class:`~src.exit.zones.trailing_ratchet.TrailingRatchetStop`)
    takes over, ratcheting up from wherever this lock left the follower.
    """

    name = "breakeven_lock"

    # Where the stop is parked, as a multiple of the live spread above break-even
    # (``level_zero``). One spread keeps a sliver of profit instead of an exact
    # break-even.
    lock_margin_spreads: float = 1.0

    # Required gap between the bid and the proposed lock level before the stop is
    # pulled up: ``noise_mult × adverse_tick_noise`` (same measure as the profit
    # trailing), floored on ``2 × spread`` so a quiet tape still needs a real move.
    noise_window: int = 20
    noise_std_k: float = 2.0
    noise_mult: float = 2.0

    def propose(self, ctx: StopContext) -> float | None:
        # The lock target: a small, fixed margin above break-even.
        target_stop = ctx.level_zero + self.lock_margin_spreads * ctx.spread

        # Up-only. The composer applies the returned level verbatim (no guard of
        # its own), so never returning a level at or below the current follower is
        # this updater's own responsibility — e.g. a follower already pushed by the
        # profit zone on an earlier excursion must not be pulled back down.
        if ctx.level_follower > 0 and target_stop <= ctx.level_follower:
            return None

        # Gate on a comfortable gap between the bid and the proposed stop. This is
        # the whole point of the margin zone: pin a hair of profit, but only once
        # the bid has run far enough that a normal pull-back would not reach the
        # stop (otherwise the trade is strangled to ~0 € the moment it is placed).
        required_gap = max(
            self.noise_mult
            * adverse_tick_noise(
                ctx.buf.bid_closes, self.noise_window, self.noise_std_k
            ),
            2.0 * ctx.spread,
        )
        if ctx.current_bid - target_stop < required_gap:
            return None

        return target_stop
