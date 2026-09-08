"""Zone 4 updater — ``trailing_noise``: hold the stop out of a normal pull-back's reach.

``trailing_ratchet`` floors its trailing distance on two measures that are both
**single-candle**: ``atr_k × ATR`` (one bar's high-low range) and
``noise_mult × adverse_tick_noise`` (what one close gives back versus the previous).
Neither sees a *multi-candle* retracement. A market that oscillates in an 8-point
band while drifting up gives back those 8 points over five or six candles of
1.5 points each — a give-back the step measure reports as 1.5 and the ATR as
about as little. The chandelier is therefore parked **inside the market's own
swing amplitude**, and because it is momentum-gated it is pushed there precisely
at the top of each micro-swing (two rising closes arm it, the swing then turns).
The next ordinary retracement takes the trade out while the move is still
running — observed live on ``CS.D.EURCAD.CFD.IP`` (2026-08-04): the follower
plateaued mid-band and the position closed at +45 € while price went on to add
another two thirds of the day's range.

``trailing_noise`` is ``trailing_ratchet`` — same support-anchored floor, same
momentum-gated chandelier, same sharp-reversal hold, same tighten-only
discipline — with **one** addition: a *retracement clearance*.

    No stop is ever left closer to the live price than the deepest pull-back the
    market has actually shown over the recent window.

Concretely, whatever the parent proposes is pulled back to at least
``retrace_mult × retracement_depth`` behind the close-out price (see
:func:`~src.core.indicators.retracement_depth`), and dropped if it then no longer
improves the follower. That is exactly the separation the premature exits call
for:

- a pull-back of **ordinary** amplitude can no longer reach the stop, so the
  trade is not closed by the noise it was opened to ride;
- a fall **deeper than every retracement of the window** is a change of regime,
  reaches the stop, and closes the position at once — the legitimate fast exit is
  untouched, because the threshold is measured from the market itself rather than
  fixed.

It only ever moves a proposed stop **away** from price, never towards it, so it
cannot make any level more fragile than the parent's; and the level it yields is
still subject to the ratchet (never loosened). One consequence is deliberate: the
pulled-back level may land **short of the margin line**, which the parent forbids
for its chandelier. It is safe here for the same reason the support-anchored floor
is allowed there — the dead-band rule exists to keep a stop out of noise's reach,
and "a full retracement behind price" *is* that test, measured on the market
rather than assumed from a fixed offset. What it means in practice is that early
in the profit zone, where the whole gain is still worth less than one retracement,
this updater reduces the risk carried instead of pretending to lock a gain the
market's own swing would take straight back. The clearance is bounded by the
position's own history — the close profile slices the buffer at the open — so in
a trade's first minutes the measured retracement is shallow and the behaviour
converges on the parent's, widening as the position accumulates its own swings.

Setting ``retrace_mult=0`` reduces this updater to ``trailing_ratchet`` exactly,
which is what the tests use to isolate the mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.exit.zones.base import StopContext
from src.exit.zones.trailing_ratchet import TrailingRatchetStop


@dataclass
class TrailingNoiseStop(TrailingRatchetStop):
    """``trailing_ratchet`` whose stop is held a full retracement behind price."""

    name = "trailing_noise"

    #: Candles scanned for the deepest give-back. Long enough to hold a few swings
    #: of the market's own oscillation, short enough that a freak pull-back rolls
    #: out of it (on 1-min data: the last half hour).
    retrace_window: int = 30
    #: Multiple of that give-back kept between the live price and the stop. ``1``
    #: means "the deepest recent retracement, repeated, would not close us";
    #: ``0`` disables the clearance and restores ``trailing_ratchet``.
    retrace_mult: float = 1.0

    def _clearance(self, ctx: StopContext) -> float:
        """Minimum gap to hold between the live price and the stop, in points."""
        if self.retrace_mult <= 0:
            return 0.0
        return self.retrace_mult * ctx.retracement(self.retrace_window)

    def propose(self, ctx: StopContext) -> float | None:
        # The parent picks the level (floor vs chandelier, momentum gate,
        # sharp-reversal hold, dead-band rule); this updater only vetoes how CLOSE
        # to price that level may sit.
        target = super().propose(ctx)
        if target is None:
            return None

        clearance = self._clearance(ctx)
        if clearance <= 0:
            return target

        # Pull the level back onto the clearance rather than holding: a stop that is
        # too close is still better than no stop at all, and the parent's guarantee
        # that entering profit always establishes a protective level must survive.
        # ``ctx.offset`` with a negative distance moves AWAY from profit — below the
        # bid for a long, above the offer for a short.
        limit = ctx.offset(ctx.current_price, -clearance)
        if ctx.beyond(target, limit):
            target = limit

        # Tighten-only, re-checked because the pull-back above can land the level at
        # or short of the follower — in which case there is nothing to post and the
        # existing (wider) follower keeps protecting the position.
        if ctx.level_follower > 0 and not ctx.beyond(target, ctx.level_follower):
            return None
        return target
