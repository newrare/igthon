"""Zone 3 updaters — price has cleared the margin but not the profit trigger.

The close-out price sits past the margin level (``level_margin``, the dotted blue
line: break-even plus one noise margin towards profit) and short of the profit
trigger (``level_profit``, the dotted green line: one further noise margin). The
move has therefore run clear of the epic's ordinary churn, but not far enough to
call it a trend — the profit trailing
(:class:`~src.exit.zones.trailing_ratchet.TrailingRatchetStop`) takes over past the
green line.

This region used to have no updater of its own: the break-even band deliberately
ran all the way to the profit trigger, so ``CLOSE_ZONEMARGE`` governed a band it
was not designed for. It is now its own zone, selected by ``CLOSE_ZONESECURE``:

- :class:`SecureHoldStop` (``hold``) — keep whatever stop the lower zones left;
- :class:`BreakevenHalfStop` (``breakeven_half``) — secure the gain immediately by
  parking the stop **halfway between break-even and the margin line**.

Both are direction-agnostic: they reason in profit terms through
:class:`~src.exit.zones.base.StopContext` (``gain`` / ``beyond`` / ``offset``), so
"raise the stop" means *towards profit* — up for a BUY, down for a SELL.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.exit.zones.base import StopContext, StopUpdater


@dataclass
class SecureHoldStop(StopUpdater):
    """Hold the stop across the margin→profit zone (no move of its own)."""

    name = "hold"

    def propose(self, ctx: StopContext) -> float | None:
        return None


@dataclass
class BreakevenHalfStop(StopUpdater):
    """Secure the gain at once, halfway between break-even and the margin line.

    Price has cleared the margin, so the trade is genuinely past the epic's noise:
    the acquired gain must be secured **immediately** rather than after a
    confirmation streak. On the very first tick spent in this zone the stop is
    moved to

    ``level_zero + support_fraction × (level_margin − level_zero)``

    — with the default :attr:`support_fraction` of ``0.5``, exactly the midpoint of
    the break-even→margin band. Because ``level_margin`` is frozen on the profit
    side of break-even, the same interpolation lands above break-even for a BUY and
    below it for a SELL.

    The level is deliberately **inside** the band rather than at the margin line:
    it locks half of the noise margin while staying far enough from the live price
    (a full noise margin below it, at minimum) that an ordinary pull-back inside
    the zone cannot reach it. It is a fixed level, not a trailing — the stop moves
    once and then stays, and the profit zone takes over past the green line.

    Two guards, as everywhere else:

    - it only fires when the level sits **strictly behind the live price** (the
      profile's software backstop closes as soon as price reaches the follower);
    - it is **tighten-only** — a follower already further into profit (the profit
      zone on a prior excursion, a manual raise, a group tightening) is never
      given back.
    """

    name = "breakeven_half"

    #: Where the stop is parked, as a fraction of the break-even→margin gap
    #: (``0 < f < 1``). ``0.5`` is the midpoint asked for by the strategy.
    support_fraction: float = 0.5

    def propose(self, ctx: StopContext) -> float | None:
        # The band must exist (margin frozen on the profit side) for the fraction
        # to mean anything.
        if ctx.gain(ctx.level_margin) <= 0:
            return None

        support = ctx.level_zero + self.support_fraction * (
            ctx.level_margin - ctx.level_zero
        )

        # Never at or past the live price: the profile's software backstop closes
        # the position as soon as price reaches the follower.
        if not ctx.beyond(ctx.current_price, support):
            return None

        # Tighten-only. The composer applies the returned level verbatim (no guard
        # of its own), so never returning a level short of the current follower is
        # this updater's own responsibility.
        if ctx.level_follower > 0 and not ctx.beyond(support, ctx.level_follower):
            return None

        return support
