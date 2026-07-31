"""Zone 1 updater — ``smartgroup``: book-wide stop tightening once the group is green.

Unlike every other :class:`~src.exit.zones.base.StopUpdater`, which reasons about
a single position, ``smartgroup`` decides for the **whole book at once**, and its
decision applies to **every open position** — winners included — not only to the
under-water ones its zone-1 slot would normally cover.

The rule, in one sentence: *if closing every open position at "its live price
minus its own noise" would already bank a net gain, then park every stop exactly
there.*

Per tick:

1. For each open position, take the live close-out price and step back one
   **noise band** — the epic's own adverse jitter
   (:func:`~src.core.indicators.adverse_tick_noise`), so the level sits just
   beyond the churn a normal tick produces. That is the position's *candidate
   stop* (:func:`candidate_stop`).
2. Value each position **at its candidate stop** in euros:
   ``sign × (candidate − level_open) × euro_per_point`` — negative for a position
   still under water, positive for one that has run.
3. Sum the book. If the total is **> 0 €**, the group is already guaranteed a net
   win at those levels, so every candidate that is a genuine tightening is applied
   at once. If the total is ≤ 0 €, nothing moves (pure ``hold``, like
   :class:`~src.exit.zones.underwater.UnderwaterStop`).

The trade this makes is deliberate: some of those tightened stops *will* be hit
and book small individual losses, but the arithmetic of step 3 guarantees the
book is green when they all are — and the ones that are not hit keep running for
more.

Two departures from a literal "price − noise":

- The cushion is floored at IG's ``min_stop_distance`` — a stop the broker would
  reject is not a stop. Widening the cushion only ever makes the estimate more
  conservative.
- A stop is never loosened: a candidate that does not sit strictly further into
  profit than the position's current follower is skipped (the position keeps the
  better stop it already has). A skipped position is then valued in step 2 **at
  the stop it actually has**, not at the candidate it will not be moved to — the
  sum only ever counts levels the book truly guarantees. A position that is both
  unmovable and unprotected (no follower at all) has unbounded downside, so it
  disarms the whole plan rather than being counted at anything.

Because the decision is portfolio-level it is computed once per monitor tick by
:func:`plan_group_tightening` (a pure function over lightweight
:class:`GroupMember` scalars) and each position is then fed its own resolved
answer through :attr:`~src.exit.zones.base.StopContext.group_tighten`.
:meth:`SmartGroupStop.propose` merely reads that pre-resolved level, so the
updater stays as pure and side-effect free as the others.

**Winners are tightened too.** The plan covers every position, so
:meth:`~src.exit.close_zoneprofit.CloseZoneProfit.evaluate` applies it in all
three zones — a position past the margin or profit line has its stop pushed to
the group level whenever that is tighter than what its own zone updater proposes.
Restricting the plan to zone 1 would leave the winners financing a tightening
they never benefit from.

The tightened stop is applied through the composer's normal ratchet path
(:meth:`~src.execution.trading.TradingService._ratchet_stop`): up-only, broker
push a spread below, min-distance clamp. So both the software follower and the
broker order move together — no extra broker code lives here.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.exit.zones.base import StopContext, StopUpdater


@dataclass(frozen=True)
class SmartGroupParams:
    """Shaping constants for :func:`plan_group_tightening`.

    Kept on a frozen dataclass (mirroring
    :class:`~src.exit.zones.base.BreakevenLockParams`) so the pure planner takes
    them explicitly and tests can vary them, while production uses the defaults.
    """

    #: Adverse-tick-noise band parameters (same measure/shape as the profit
    #: trailing floor). The band is the distance the candidate stop is stepped
    #: back from the live price — "as close to the noise as possible without
    #: sitting inside it", so an ordinary tick does not trigger it.
    noise_window: int = 20
    noise_std_k: float = 2.0
    #: Euros the estimated book total must **exceed** for the plan to arm. ``0.0``
    #: is the rule as specified (arm on any net gain); raise it to demand a margin
    #: for spread/slippage before the group commits.
    min_group_euro: float = 0.0


@dataclass(slots=True)
class GroupMember:
    """Per-position scalars the group planner needs — one per open position.

    Deliberately holds only plain numbers (no ORM row, no buffer) so
    :func:`plan_group_tightening` is a pure, fully unit-testable function. Built by
    :meth:`~src.exit.close_zoneprofit.CloseZoneProfit.group_member` from the
    persisted position plus the live buffer.

    Longs and shorts are valued in one pass: every quantity below is either a euro
    amount (already side-free) or a price compared through :attr:`sign`.

    Attributes:
        position_id: Stable key used to return the decision to the right position.
        level_open: Fill level of the position — the P&L reference every euro
            estimate is measured from.
        level_follower: Current software stop; a candidate must beat it to be a
            genuine tightening.
        euro_per_point: Euro P&L per point of price move (position size).
        current_price: Live close-out price (bid for a BUY, offer for a SELL).
        min_stop_distance: IG minimum stop distance in price units (0 when unknown).
        noise: Adverse-tick-noise band — the step back from the live price.
        sign: ``+1`` for a BUY, ``−1`` for a SELL — the direction profit moves in.
    """

    position_id: int
    level_open: float
    level_follower: float
    euro_per_point: float
    current_price: float
    min_stop_distance: float
    noise: float
    sign: float = 1.0


def candidate_stop(member: GroupMember) -> float:
    """The member's live price stepped back one cushion, in the adverse direction.

    The cushion is the epic's adverse-tick-noise band, floored at IG's minimum
    stop distance so the level is one the broker would actually accept. For a BUY
    that lands below the bid, for a SELL above the offer.
    """
    return member.current_price - member.sign * max(
        member.noise, member.min_stop_distance
    )


def _euro_at(member: GroupMember, level: float) -> float:
    """Euro P&L this position books if it is closed at ``level``."""
    return member.sign * (level - member.level_open) * member.euro_per_point


def _tightens(member: GroupMember, candidate: float) -> bool:
    """True when ``candidate`` is a stop this position may legally be moved to.

    Two ways it is not: the cushion collapsed to nothing, so the stop would sit on
    the live price and the profile's software backstop would close the position on
    the spot; or the current follower already sits further into profit, and stops
    are never loosened.
    """
    if member.sign * (member.current_price - candidate) <= 0:
        return False
    return (
        member.level_follower <= 0
        or member.sign * (candidate - member.level_follower) > 0
    )


def plan_group_tightening(
    members: list[GroupMember], params: SmartGroupParams | None = None
) -> dict[int, float]:
    """Tighten every stop onto ``price − noise`` when that book total is positive.

    Pure function (see the module docstring for the rule). Returns a mapping of
    ``position_id -> absolute stop level`` covering **every** position whose
    candidate is a legal tightening; an empty map means the book is not green at
    those levels and nothing moves this tick.

    The steps:

    1. **Value the book at the stops it would really have** — a member that can
       legally be moved onto its :func:`candidate_stop` (see :func:`_tightens`) is
       valued there, whichever zone it currently sits in; one that cannot is valued
       at the follower it keeps. Valuing a skipped member at its candidate is the
       one thing that would break the whole argument: a position on a flat plateau
       (noise 0, no broker minimum) cannot be tightened at all, and counting its
       full paper profit would claim a gain that nothing in the book protects.
       Members with no usable size or fill level are ignored entirely (they cannot
       be valued, so they neither arm the plan nor ride it); a member that can
       neither be tightened nor already has a stop disarms the plan outright.
    2. **Gate on the total** — the plan arms only when the sum exceeds
       :attr:`~SmartGroupParams.min_group_euro` (0 € by default). This is the whole
       safety argument, and it now holds by construction: every euro in the sum sits
       behind a stop that is either already resting or about to be placed.
    3. **Apply the candidates** — every member valued at its candidate in step 1 is
       returned for tightening; the others keep what they have.
    """
    params = params or SmartGroupParams()

    total = 0.0
    plan: dict[int, float] = {}
    for m in members:
        # A member with no size or no fill level cannot be valued in euros; letting
        # it through would let a data gap fake (or hide) a group gain.
        if m.euro_per_point <= 0 or m.level_open <= 0:
            continue
        candidate = candidate_stop(m)
        if _tightens(m, candidate):
            plan[m.position_id] = candidate
            total += _euro_at(m, candidate)
            continue
        # Skipped: this position keeps the stop it already has, so THAT is what it
        # contributes to the book — never the candidate it will not be moved to.
        if m.level_follower <= 0:
            # No stop at all and none can be placed: its downside is unbounded, so
            # the "the book is green" claim cannot be made at all this tick.
            return {}
        total += _euro_at(m, m.level_follower)

    if total <= params.min_group_euro:
        return {}
    return plan


@dataclass
class SmartGroupStop(StopUpdater):
    """Zone-1 updater that tightens the whole book once it is green net of noise.

    See the module docstring for the algorithm. ``propose`` is a pure reader of the
    pre-resolved :attr:`~src.exit.zones.base.StopContext.group_tighten`; the
    portfolio maths runs upstream in :func:`plan_group_tightening` via
    :meth:`plan`.
    """

    name = "smartgroup"

    params: SmartGroupParams = SmartGroupParams()

    def plan(self, members: list[GroupMember]) -> dict[int, float]:
        """Compute the per-tick group tightening plan (delegates to the pure fn)."""
        return plan_group_tightening(members, self.params)

    def propose(self, ctx: StopContext) -> float | None:
        # The group pre-pass already decided this position's tightened stop (or
        # None to hold). The composer applies the up-only / min-distance guards.
        return ctx.group_tighten
