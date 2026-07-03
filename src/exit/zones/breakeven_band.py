"""Zone 2 updaters — the bid is in the noise band just above break-even.

The bid sits above break-even (``level_zero``) but has not yet cleared the margin
level (``level_zero + noise_margin``). This is the delicate region: parking the
stop a hair above break-even here is exactly where ordinary bid/offer noise alone
would trigger it for ~zero profit (the "everything exits at 0 €" pathology that a
naive break-even pin caused live).

Two updaters live here, selected by ``CLOSE_ZONEMARGE``:

- :class:`BreakevenBandStop` (``hold``) — leave the initial stop untouched; the
  stop only ever moves once the bid clears the margin level (zone 3);
- :class:`BreakevenLockStop` (``breakeven_lock``) — pull the stop up under the
  recent swing low **once the move has genuinely held above break-even** (a
  persistence-and-noise gate), so a normal pull-back cannot immediately knock it
  out. This secures a hair of profit early on a fast reversal without hugging the
  bid.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.exit.zones.base import (
    BreakevenLockParams,
    StopContext,
    StopUpdater,
    breakeven_lock_level,
)


@dataclass
class BreakevenBandStop(StopUpdater):
    """Hold the stop while the bid is in the noise band above break-even."""

    name = "hold"

    def propose(self, ctx: StopContext) -> float | None:
        return None


@dataclass
class BreakevenLockStop(StopUpdater):
    """Lock the stop under the recent swing low once the move holds above break-even.

    The old design pinned the stop a fixed ``spread`` above break-even and only
    fired once the bid ran a noise-sized gap clear of it. That gap
    (``level_zero + 3 × spread`` on a quiet tape) sat **above** the margin level
    whenever the noise margin was thin, so the bid left the band into the profit
    zone before the gate ever opened — a firing region that was empty by
    construction, and the lock never engaged (observed live on CS.D.EURCAD.CFD.IP).

    This replaces the fixed gap with two changes:

    - **trigger** — a persistence-and-noise gate: the recent swing low, net of the
      adverse-tick-noise band, must sit above break-even (see
      :func:`~src.exit.zones.base.breakeven_lock_level`). This is meetable *inside*
      the band: it only asks that the move has genuinely held above break-even,
      not that the bid has run three spreads clear;
    - **level** — the stop is anchored under that real swing low rather than at a
      fixed spread offset, so ordinary noise cannot reach it.

    The stop is a *lock*, not a trailing. Once the bid clears the margin level the
    position enters zone 3 and the profit trailing
    (:class:`~src.exit.zones.trailing_ratchet.TrailingRatchetStop`) takes over,
    using the *same* lock as its floor (via ``breakeven_lock_level``) so the
    follower keeps climbing on one continuous curve across the two zones.
    """

    name = "breakeven_lock"

    #: Shared shaping of the support-anchored lock (see ``breakeven_lock_level``).
    lock: BreakevenLockParams = field(default_factory=BreakevenLockParams)

    def propose(self, ctx: StopContext) -> float | None:
        target = breakeven_lock_level(ctx, self.lock)
        if target is None:
            return None

        # Up-only. The composer applies the returned level verbatim (no guard of
        # its own), so never returning a level at or below the current follower is
        # this updater's own responsibility — e.g. a follower already pushed by the
        # profit zone on an earlier excursion must not be pulled back down.
        if ctx.level_follower > 0 and target <= ctx.level_follower:
            return None

        return target
