"""Zone 1 updaters — price has not cleared break-even.

Two updaters live here, selected by ``CLOSE_ZONESTART``:

- :class:`UnderwaterStop` (``hold``) — **hold the initial protective stop
  untouched**. The stop posted at open is never loosened and never nudged while
  the trade is underwater; it is left to the broker to fill it if price runs
  that far. This preserves the profit-gated profile's rule of not touching the
  stop until the trade is genuinely in profit.
- :class:`UnderwaterTrendCutStop` (``trendcut``) — **tighten the stop toward
  break-even once the move since open is a clean, confirmed adverse trend**, so a
  trade that is demonstrably wrong is cut for a fraction of the planned risk
  instead of riding the full initial stop out to ``-1R``. A choppy, directionless
  drift leaves the wide initial stop in place (that stop's whole job is to survive
  noise), so this only ever bites the monotone straight-to-stop losers.

Both read direction through :class:`~src.exit.zones.base.StopContext`, so
"adverse" means falling for a BUY and rising for a SELL.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.indicators import efficiency_ratio, linear_regression
from src.exit.zones.base import StopContext, StopUpdater


@dataclass
class UnderwaterStop(StopUpdater):
    """Hold the initial stop while price has not cleared break-even."""

    name = "hold"

    def propose(self, ctx: StopContext) -> float | None:
        return None


@dataclass
class UnderwaterTrendCutStop(StopUpdater):
    """Tighten the stop toward break-even on a clean, confirmed adverse trend.

    While the bid is underwater (``bid <= level_zero``) the initial stop normally
    holds untouched (:class:`UnderwaterStop`) and a trade that goes straight
    against the entry rides it all the way down to the full planned risk. That
    monotone straight-to-stop path is the single largest source of realised
    losses — and it is exactly the case where holding the wide stop adds no value,
    because the market is not oscillating around the entry, it is leaving it.

    This updater cuts that path short. Once the move since open is a **clean,
    directional adverse trend** it tightens the stop to a fraction of the initial
    risk short of break-even, so the loser is closed near ``-cut_fraction × R``
    instead of ``-1R``. Two independent gates keep it off ordinary noise:

    - **direction** — the least-squares slope of the close-out prices since open
      must be adverse (price is genuinely against the entry, not merely wobbling);
    - **cleanliness** — the Kaufman efficiency ratio over the same window must be
      at least :attr:`er_min`. A choppy, mean-reverting drift (``ER`` low) is left
      on the wide initial stop, which exists precisely to survive that noise; only
      a decisive one-way move (``ER`` high) is cut. A trade that first ran our way
      and then reversed scores a low ``ER`` here (small net move over a long path),
      so it too keeps the wide stop rather than being knifed on the pull-back.

    The tightened stop is placed ``cut_fraction × R`` short of break-even (with
    ``R`` the initial break-even→stop distance), but never **through the live
    price** — a broker stop cannot sit in the market, so when price has already run
    past that level the stop is parked one spread behind price (an all-but-immediate
    cut). The move is **tighten-only**: it never loosens the stop, and it does
    nothing once the follower already sits at or past break-even (a prior excursion
    locked a level there and its own backstop governs), so it can only ever *reduce*
    a loss.
    """

    name = "trendcut"

    #: Minimum recorded closes since open before a cut is considered — enough of a
    #: window to trust the slope/ER read rather than react to the first few ticks.
    min_ticks: int = 15
    #: Minimum efficiency ratio of the since-open window that qualifies the move as
    #: a clean one-way trend (below this the drift is treated as noise and held).
    er_min: float = 0.5
    #: Where the tightened stop sits, as a fraction of the initial risk short of
    #: break-even. ``0.5`` cuts the loser at roughly half the planned ``-1R``.
    cut_fraction: float = 0.5

    def propose(self, ctx: StopContext) -> float | None:
        # Initial risk in price terms (break-even out to the initial stop). A
        # follower at or past break-even means a prior excursion already locked a
        # level there — nothing to tighten, its own backstop governs.
        risk = -ctx.gain(ctx.level_follower)
        if risk <= 0:
            return None

        closes = ctx.favourable_closes
        if len(closes) < self.min_ticks + 1:
            return None

        # Direction gate: the move since open must genuinely be against us, not
        # wobbling (the sign-normalised series falls when the trade goes wrong).
        if linear_regression(closes).slope >= 0:
            return None

        # Cleanliness gate: only a decisive one-way move is cut; choppy drift keeps
        # the wide initial stop that exists to survive exactly that noise.
        if efficiency_ratio(closes, len(closes) - 1) < self.er_min:
            return None

        cut_level = ctx.offset(ctx.level_zero, -self.cut_fraction * risk)
        # Never sit the stop in the market: when price has already run past the cut
        # level, park it one spread behind price (all-but-immediate cut) rather than
        # returning a level a broker stop could not hold. "Behind" = the candidate
        # that is *less* far into profit of the two.
        target = min(
            (cut_level, ctx.offset(ctx.current_price, -ctx.spread)), key=ctx.gain
        )

        # Tighten-only. The composer applies the returned level verbatim, so refusing
        # to loosen (or merely re-post) the stop is this updater's own responsibility.
        if not ctx.beyond(target, ctx.level_follower):
            return None

        return target
