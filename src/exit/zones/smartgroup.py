"""Zone 1 updater — ``smartgroup``: portfolio-aware loss capping.

Unlike every other :class:`~src.exit.zones.base.StopUpdater`, which reasons about
a single position, ``smartgroup`` manages the still-negative positions using the
state of the **whole book**. It behaves like :class:`~src.exit.zones.underwater.
UnderwaterStop` (pure ``hold``) by default, but once the portfolio has
**guaranteed euro capital** locked in — positions whose stop already sits above
break-even — it spends a fraction of that guaranteed pot to tighten the stops of
the underwater positions **toward the bid**, capping their downside without
closing them (they may still rally). It only ever tightens while the group stays
net-positive by construction.

The decision is genuinely portfolio-level, so it is computed once per monitor
tick by :func:`plan_group_tightening` (a pure function over lightweight
:class:`GroupMember` scalars) and each position is then fed its own resolved
answer through :attr:`~src.exit.zones.base.StopContext.group_tighten`. The
updater's :meth:`SmartGroupStop.propose` merely reads that pre-resolved level, so
it stays as pure and side-effect free as the others.

The tightened stop is applied through the composer's normal ratchet path
(:meth:`~src.execution.trading.TradingService._ratchet_stop`): up-only, broker
push a spread below, min-distance clamp. So both the software follower and the
broker order move together — no extra broker code lives here.

Three shaping rules (all class constants on :class:`SmartGroupParams`):

- **Budget fraction** — only ``budget_fraction`` (0.8) of the guaranteed pot is
  usable as a loss budget, leaving a safety margin so the group stays net-positive
  even after spreads/slippage.
- **Budget-adaptive cushion** — the richer the budget relative to the exposure,
  the *wider* the cushion behind price (more room to breathe); a thin budget
  collapses the cushion onto the noise band (``adverse_tick_noise``) — just beyond
  ordinary jitter, so a normal tick does not trigger it.
- **Priority-greedy arming** — negatives are ranked by *loss-reduction
  efficiency* (euros of downside capped per euro of budget spent) and tightened
  best-first until the budget is exhausted: a small tighten that saves a lot wins
  the budget over a big tighten that saves little.
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

    #: Fraction of the guaranteed pot usable as a loss budget (the rest is a
    #: safety cushion). ``0.8`` = spend at most 80 % of locked-in profit.
    budget_fraction: float = 0.8
    #: Widest cushion below the bid, as a multiple of ATR, reached at full budget
    #: richness. The cushion interpolates from the noise floor up to this.
    max_cushion_atr: float = 1.0
    #: Adverse-tick-noise band parameters (same measure/shape as the profit
    #: trailing floor) used as the tightest safe cushion — "close to the noise
    #: without touching it".
    noise_window: int = 20
    noise_std_k: float = 2.0


@dataclass(slots=True)
class GroupMember:
    """Per-position scalars the group planner needs — one per open position.

    Deliberately holds only plain numbers (no ORM row, no buffer) so
    :func:`plan_group_tightening` is a pure, fully unit-testable function. Built by
    :meth:`~src.exit.close_zoneprofit.CloseZoneProfit.group_member` from the
    persisted position plus the live buffer.

    Longs and shorts share one pot: every quantity below is either a euro amount
    (already side-free) or a price compared through :attr:`sign`, so a book mixing
    both sides is planned in a single pass.

    Attributes:
        position_id: Stable key used to return the decision to the right position.
        level_open: Fill level of the position (P&L reference).
        level_zero: Break-even level (entry offer for a BUY, entry bid for a SELL)
            — the winner boundary.
        level_follower: Current software stop; the level a winner's guaranteed
            euro is measured at, and the floor an underwater stop tightens from.
        euro_per_point: Euro P&L per point of price move (position size).
        current_price: Live close-out price (bid for a BUY, offer for a SELL).
        atr_value: Recent ATR (sizes the widest cushion).
        spread: Live bid/offer spread.
        min_stop_distance: IG minimum stop distance in price units (0 when unknown).
        noise: Adverse-tick-noise band (tightest safe cushion floor).
        sign: ``+1`` for a BUY, ``−1`` for a SELL — the direction profit moves in.
    """

    position_id: int
    level_open: float
    level_zero: float
    level_follower: float
    euro_per_point: float
    current_price: float
    atr_value: float
    spread: float
    min_stop_distance: float
    noise: float
    sign: float = 1.0


def _clamp(value: float, low: float, high: float) -> float:
    """Clamp ``value`` into ``[low, high]``."""
    return max(low, min(high, value))


def plan_group_tightening(
    members: list[GroupMember], params: SmartGroupParams | None = None
) -> dict[int, float]:
    """Decide which underwater positions to tighten, and to what stop level.

    Pure function (see the module docstring for the rules). Returns a mapping of
    ``position_id -> absolute stop level`` for the positions selected for
    tightening this tick; positions not in the map hold their current stop.

    The steps (a deliberately **two-phase, non-circular** design — select cheaply
    first, then spend what is left on width):

    1. **Guaranteed pot** — sum the locked-in profit of the winners (stop at or
       above break-even). ``budget = budget_fraction × pot``. A non-positive
       budget disarms everything (pure hold, the open-time default).
    2. **Floor candidates** — each underwater loser gets its tightest safe stop
       first: a cushion at the noise floor (``max(noise, min_stop_distance)``).
       Drop the ones where that is not a genuine up-only tightening below the bid.
    3. **Greedy selection at the floor** — rank survivors by loss-reduction
       efficiency (euros of downside capped vs. riding the wide initial stop, per
       euro of budget spent) and select best-first while the cumulative floor cost
       stays within budget. This maximises how many positions are protected.
    4. **Widen with the surplus** — any budget left after the floor selection buys
       breathing room: the selected cushions widen uniformly from the floor toward
       ``max_cushion_atr × ATR`` (never past the initial stop), by exactly the
       fraction of the widening cost the surplus can pay for. A thin budget leaves
       no surplus, so the stops stay pinned on the noise floor; a rich budget
       widens them fully.
    """
    params = params or SmartGroupParams()

    # 1. Guaranteed pot from the winners (stop already at/past break-even). Both
    # quantities are signed by the member's direction, so a short's stop sitting
    # BELOW its entry contributes the same positive euros as a long's above.
    guaranteed = sum(
        m.sign * (m.level_follower - m.level_open) * m.euro_per_point
        for m in members
        if m.sign * (m.level_follower - m.level_zero) >= 0 and m.euro_per_point > 0
    )
    budget = params.budget_fraction * guaranteed
    if budget <= 0:
        return {}

    # 2. Losers = underwater positions (price has not cleared break-even) whose
    # floor cushion is a genuine tightening sitting strictly behind the live price.
    def floor(m: GroupMember) -> float:
        return max(m.noise, m.min_stop_distance)

    def cost_of(m: GroupMember, stop: float) -> float:
        # Euros lost if the tightened stop is hit (positive for an underwater stop).
        return m.sign * (m.level_open - stop) * m.euro_per_point

    candidates = []  # (efficiency, floor_cost, member, floor_stop, max_cushion)
    for m in members:
        if m.euro_per_point <= 0 or m.sign * (m.current_price - m.level_zero) > 0:
            continue
        floor_c = floor(m)
        floor_stop = m.current_price - m.sign * floor_c
        if (
            m.sign * (floor_stop - m.level_follower) <= 0
            or m.sign * (m.current_price - floor_stop) <= 0
        ):
            continue
        floor_cost = cost_of(m, floor_stop)
        saving = m.sign * (floor_stop - m.level_follower) * m.euro_per_point
        # Efficiency = downside capped per euro of budget spent. A non-positive
        # cost means the stop already locks break-even-or-better: free protection,
        # ranked ahead of everything (sentinel infinity).
        efficiency = saving / floor_cost if floor_cost > 0 else float("inf")
        # The widest the cushion may ever grow: the ATR band, but never so far that
        # the widened stop falls back to/behind the initial follower (that would undo
        # the tightening rather than add breathing room), and never narrower than the
        # floor (a tiny ATR must not pull the cap inside the noise floor).
        max_cushion = max(
            floor_c,
            min(
                params.max_cushion_atr * m.atr_value,
                m.sign * (m.current_price - m.level_follower),
            ),
        )
        candidates.append(
            (efficiency, max(0.0, floor_cost), m, floor_stop, max_cushion)
        )

    if not candidates:
        return {}

    # 3. Greedy by efficiency (desc); select at the floor while the budget holds.
    candidates.sort(key=lambda c: c[0], reverse=True)
    selected = []  # (member, floor_cushion, max_cushion)
    spent = 0.0
    for _efficiency, floor_cost, m, floor_stop, max_cushion in candidates:
        if spent + floor_cost > budget:
            continue
        selected.append((m, m.sign * (m.current_price - floor_stop), max_cushion))
        spent += floor_cost
    if not selected:
        return {}

    # 4. Spend the surplus on width, uniformly across the selected positions. The
    # extra euro cost of widening loser i from its floor to its max cushion is
    # ``(max_cushion - floor_cushion) × euro_per_point``; ``widen`` is the fraction
    # of that total the surplus can pay for, so the total stays within budget.
    surplus = budget - spent
    widen_cost = sum(
        (max_c - floor_c) * m.euro_per_point for m, floor_c, max_c in selected
    )
    widen = 1.0 if widen_cost <= 0 else _clamp(surplus / widen_cost, 0.0, 1.0)

    plan: dict[int, float] = {}
    for m, floor_c, max_c in selected:
        cushion = floor_c + widen * (max_c - floor_c)
        plan[m.position_id] = m.current_price - m.sign * cushion
    return plan


@dataclass
class SmartGroupStop(StopUpdater):
    """Zone-1 updater that tightens underwater stops using the whole-book budget.

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
