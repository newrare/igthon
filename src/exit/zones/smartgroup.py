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
2. Value each position **at the fill its candidate stop would get** in euros:
   ``sign × (fill − level_open) × euro_per_point`` — negative for a position
   still under water, positive for one that has run. The fill is the stop level
   moved one execution haircut the adverse way, and the result is then shaved by
   a reconciliation margin (see "Two haircuts" below).
3. Sum the book. If the total is **> 0 €**, the group is already guaranteed a net
   win at those levels, so every candidate that is a genuine tightening is applied
   at once. If the total is ≤ 0 €, nothing moves (pure ``hold``, like
   :class:`~src.exit.zones.underwater.UnderwaterStop`).

The trade this makes is deliberate: some of those tightened stops *will* be hit
and book small individual losses, but the arithmetic of step 3 guarantees the
book is green when they all are — and the ones that are not hit keep running for
more.

**Two haircuts.** Valuing the book at the stop levels themselves is
systematically optimistic, and the error is not small: a live case had a winner
counted at +99.00 € on a follower it never traded at, then booked +64.84 € when
that stop fired — 35 % of its contribution lost between the level and the fill,
enough on its own to turn a "+19.29 € green book" into a losing one. Two
corrections, both applied to the valuation only (never to the stop levels the
plan actually places):

- **Execution slip** (:attr:`SmartGroupParams.exec_slip_k`) — a stop never fills
  where it sits. The software follower is only tested between two polls and then
  closes at market, and the resting broker order deliberately sits one spread
  plus a noise cushion further out, so the real fill lands somewhere in that gap.
  Each position is therefore valued that fraction of the gap beyond its stop.
- **Reconciliation margin** (:attr:`SmartGroupParams.reconcile_margin_pct`) — a
  flat percentage off every member's absolute euro figure, covering the drift
  between our ``euro_per_point`` arithmetic and what IG finally books (FX
  conversion at close, fees, rounding).

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
:func:`explain_group_tightening` (a pure function over lightweight
:class:`GroupMember` scalars) and each position is then fed its own resolved
answer through :attr:`~src.exit.zones.base.StopContext.group_tighten`. That
function returns a :class:`GroupPlanReport` — the plan **plus** the book total it
was gated on and how each position was valued — because a tick where the gate is
missed produces an empty plan, which on its own is indistinguishable from the
pre-pass never running; the monitor logs the report for exactly that reason.
:func:`plan_group_tightening` is the thin wrapper that keeps only the plan.
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
    #: Share of the follower→broker-stop gap assumed lost on the exit fill (see
    #: :attr:`GroupMember.exec_slip`). A stop exit never fills at the level the
    #: stop sits on: the software follower is only checked between two polls and
    #: then closes at market, and the resting broker order deliberately sits one
    #: spread plus a noise cushion further out. ``0.0`` values every position at
    #: its stop level (the pre-haircut behaviour, systematically optimistic);
    #: ``1.0`` values it at the broker order — the worst level the book can fill
    #: at, barring a gap.
    exec_slip_k: float = 0.5
    #: Fraction of each member's euro estimate discarded to cover the gap between
    #: our own ``euro_per_point`` arithmetic and what IG actually books (FX
    #: conversion, fees, rounding). Applied on the **absolute** value, so it makes
    #: winners smaller and losers bigger — pessimistic on both sides.
    reconcile_margin_pct: float = 0.02


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
        exec_slip: Price distance a stop exit is assumed to lose against the level
            the stop rests on — the execution haircut, in the adverse direction.
            Zero means "the fill lands exactly on the stop", which never happens
            in practice (see :attr:`SmartGroupParams.exec_slip_k`).
    """

    position_id: int
    level_open: float
    level_follower: float
    euro_per_point: float
    current_price: float
    min_stop_distance: float
    noise: float
    sign: float = 1.0
    exec_slip: float = 0.0


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


def _fill_level(member: GroupMember, stop_level: float) -> float:
    """The level a stop resting at ``stop_level`` is assumed to actually fill at.

    One :attr:`GroupMember.exec_slip` further in the adverse direction. The book
    is a promise about money, not about levels, so it must be valued at the fill
    the exit really gets — never at the level the stop is parked on, which is
    unreachable by construction (the follower closes at market one poll later,
    the broker order sits a spread plus a cushion beyond it).
    """
    return stop_level - member.sign * max(member.exec_slip, 0.0)


def _haircut(euro: float, pct: float) -> float:
    """Shave ``pct`` off an euro estimate, pessimistically on both signs.

    Our ``euro_per_point`` arithmetic and IG's booked figure never agree to the
    cent (FX conversion at close, fees, rounding). Trimming the **absolute**
    value makes a gain count for less and a loss count for more, so the gap can
    only ever surprise the book in its favour.
    """
    return euro - abs(euro) * max(pct, 0.0)


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


@dataclass(frozen=True)
class MemberValuation:
    """How one member was valued by :func:`explain_group_tightening`.

    Diagnostic only — the plan itself is just ``position_id -> candidate``. This
    records the arithmetic behind that decision so a caller can log *why* the book
    did (or did not) arm, without re-deriving it.

    Attributes:
        position_id: The member's :attr:`GroupMember.position_id`.
        candidate: Its :func:`candidate_stop` for this tick.
        level_follower: The software stop it currently rests on (0 when none).
        tightens: True when the candidate is a legal tightening (see
            :func:`_tightens`) — i.e. this member is in the plan.
        valued_at: The level its euro contribution was measured at — the stop it
            would rest on (the candidate when ``tightens``, else the follower it
            keeps) moved one :attr:`GroupMember.exec_slip` the adverse way, i.e.
            where that stop is assumed to actually fill.
        euro: That contribution, in euros, net of the execution haircut and the
            reconciliation margin.
    """

    position_id: int
    candidate: float
    level_follower: float
    tightens: bool
    valued_at: float
    euro: float


@dataclass(frozen=True)
class GroupPlanReport:
    """The group decision plus the numbers it was taken on.

    Attributes:
        plan: ``position_id -> absolute stop level``, empty when nothing moves.
        total_euro: The book valued at the stops it would really have.
        gate_euro: The threshold ``total_euro`` had to exceed
            (:attr:`SmartGroupParams.min_group_euro`).
        armed: True when the gate was cleared, so :attr:`plan` was applied.
        valuations: One entry per priced member, in input order.
        unpriceable: Members skipped entirely (no size or no fill level).
        disarmed_by: The position whose missing stop voided the whole plan, if any
            — when set, ``total_euro`` is a partial sum and means nothing.
    """

    plan: dict[int, float]
    total_euro: float
    gate_euro: float
    armed: bool
    valuations: list[MemberValuation]
    unpriceable: list[int]
    disarmed_by: int | None = None


def explain_group_tightening(
    members: list[GroupMember], params: SmartGroupParams | None = None
) -> GroupPlanReport:
    """Run the group rule and return the decision **with** its arithmetic.

    Pure function (see the module docstring for the rule);
    :func:`plan_group_tightening` is the thin wrapper that keeps only the plan.
    The report exists because the gate is a book-wide claim that holds or fails on
    numbers no single position can show: without it, a tick where nothing moves is
    indistinguishable from the mechanism not running at all.

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
       safety argument, and it holds by construction: every euro in the sum sits
       behind a stop that is either already resting or about to be placed.
    3. **Apply the candidates** — every member valued at its candidate in step 1 is
       returned for tightening; the others keep what they have.
    """
    params = params or SmartGroupParams()

    total = 0.0
    plan: dict[int, float] = {}
    valuations: list[MemberValuation] = []
    unpriceable: list[int] = []
    for m in members:
        # A member with no size or no fill level cannot be valued in euros; letting
        # it through would let a data gap fake (or hide) a group gain.
        if m.euro_per_point <= 0 or m.level_open <= 0:
            unpriceable.append(m.position_id)
            continue
        candidate = candidate_stop(m)
        tightens = _tightens(m, candidate)
        # A skipped member keeps the stop it already has, so THAT is what it
        # contributes to the book — never the candidate it will not be moved to.
        if not tightens and m.level_follower <= 0:
            # No stop at all and none can be placed: its downside is unbounded, so
            # the "the book is green" claim cannot be made at all this tick.
            return GroupPlanReport(
                plan={},
                total_euro=total,
                gate_euro=params.min_group_euro,
                armed=False,
                valuations=valuations,
                unpriceable=unpriceable,
                disarmed_by=m.position_id,
            )
        level = candidate if tightens else m.level_follower
        # Value the exit where it really fills, then shave the reconciliation
        # margin — both haircuts are pessimistic, so the gate below stays a claim
        # the book can actually honour.
        valued_at = _fill_level(m, level)
        euro = _haircut(_euro_at(m, valued_at), params.reconcile_margin_pct)
        total += euro
        if tightens:
            plan[m.position_id] = candidate
        valuations.append(
            MemberValuation(
                position_id=m.position_id,
                candidate=candidate,
                level_follower=m.level_follower,
                tightens=tightens,
                valued_at=valued_at,
                euro=euro,
            )
        )

    armed = total > params.min_group_euro
    return GroupPlanReport(
        plan=plan if armed else {},
        total_euro=total,
        gate_euro=params.min_group_euro,
        armed=armed,
        valuations=valuations,
        unpriceable=unpriceable,
    )


def plan_group_tightening(
    members: list[GroupMember], params: SmartGroupParams | None = None
) -> dict[int, float]:
    """Tighten every stop onto ``price − noise`` when that book total is positive.

    Returns a mapping of ``position_id -> absolute stop level`` covering **every**
    position whose candidate is a legal tightening; an empty map means the book is
    not green at those levels and nothing moves this tick. See
    :func:`explain_group_tightening` for the rule and for the same decision with
    the numbers behind it attached.
    """
    return explain_group_tightening(members, params).plan


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

    def explain(self, members: list[GroupMember]) -> GroupPlanReport:
        """Same plan as :meth:`plan`, with the book arithmetic behind it."""
        return explain_group_tightening(members, self.params)

    def propose(self, ctx: StopContext) -> float | None:
        # The group pre-pass already decided this position's tightened stop (or
        # None to hold). The composer applies the up-only / min-distance guards.
        return ctx.group_tighten
