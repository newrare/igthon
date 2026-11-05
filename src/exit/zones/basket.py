"""Zone 1 updater — ``basket``: close the whole book once it is green enough.

The exit half of the daily basket (:class:`~src.entry.open_basket.OpenBasket`).
The three positions are opened together and judged together: none of them is
managed for its own sake. Like ``smartgroup`` the decision is taken once per
monitor tick for **every** open position at once, through the composer's group
pre-pass; unlike it, the decision is not a stop move but a close.

Per tick, on the Paris wall clock:

1. **The book is green enough** — the open positions valued at their live
   close-out price (the spread already paid), plus what the day has already
   banked (a leg stopped out earlier stays in the sum), exceed the target: every
   position is closed at once (``basket_win``).
2. **11:00 or later and still open** — the report flags a review: the scheduler
   notifies the user once, who decides for the day. The automatic close keeps
   running meanwhile; nothing else changes.
3. **17:30 or later and still open** — the report flags a close alert: the
   scheduler notifies the user once that the basket is about to be carried past
   the session. Nothing is closed by the clock: the user closes by hand or lets
   the book run to its target or its stops.
4. Otherwise everything holds — the far initial stop (``stop_far``) is the only
   protection, and :meth:`BasketCloseStop.propose` never moves it.

The target is **one quarter-hour ATR** of the basket, in euros. Each leg's stop
sits :data:`~src.stops.stop_far.FAR_STOP_ATR_K` quarter-hour ATRs away, so one
ATR15 of that leg is worth ``euro_stop / FAR_STOP_ATR_K`` euros, and the target is
that figure averaged over the open legs — the "+1 ATR15" rule of the daily-basket
replay. The replay weighted the three legs equally; live, every leg is opened at
IG's minimum deal size, so a leg whose ATR15 is worth more euros weighs more in
the sum. A position whose ``euro_stop`` is unknown (or
whose size or open level is missing) makes the target unknowable, so the book is
then never closed on the target, only by its stops or by hand.

Each member's euro figure is shaved by a small reconciliation margin, as in
``smartgroup``: our ``euro_per_point`` arithmetic never matches what IG books to
the cent, and the claim "the book is green" must survive that gap.

Measured on the replay (2026-10-23, W24–W42, ``open_basket`` selection, stop
8 ATR15): the book touched a positive sum on 93 % of days, but so did a coin flip
(87 %); closing at +1 ATR15 averaged +0.14 ATR15 per day, not significant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from src.exit.zones.base import StopContext, StopUpdater
from src.exit.zones.smartgroup import GroupMember, GroupPlanReport, MemberValuation
from src.stops.stop_far import FAR_STOP_ATR_K

PARIS = ZoneInfo("Europe/Paris")

#: ``reason_close`` of a basket closed on its target.
BASKET_WIN_REASON = "basket_win"


@dataclass(frozen=True)
class BasketParams:
    """Shaping constants for :func:`explain_basket`."""

    #: Book target, in quarter-hour ATRs of the basket (see the module docstring).
    target_atr: float = 1.0
    #: Distance of the initial stop in quarter-hour ATRs — what converts a leg's
    #: euro risk into the euro value of one ATR15. Must match ``stop_far``.
    stop_atr_k: float = FAR_STOP_ATR_K
    #: Paris time from which a still-open basket asks for the user's decision.
    review_at: time = time(11, 0)
    #: Paris time from which a still-open basket raises the close alert.
    alert_at: time = time(17, 30)
    #: Fraction of each member's absolute euro figure discarded (see
    #: :attr:`~src.exit.zones.smartgroup.SmartGroupParams.reconcile_margin_pct`).
    reconcile_margin_pct: float = 0.02


def basket_target_euro(
    members: list[GroupMember], params: BasketParams | None = None
) -> float | None:
    """The euro gain the book must exceed to be closed, or ``None`` if unknown.

    ``target_atr`` quarter-hour ATRs of an average leg: the mean ``euro_risk``
    of the open members divided by the stop's ATR multiple. ``None`` when there
    is no member or one of them has no euro risk on record.
    """
    params = params or BasketParams()
    if not members or params.stop_atr_k <= 0:
        return None
    risks = [m.euro_risk for m in members]
    if any(r <= 0 for r in risks):
        return None
    return params.target_atr * (sum(risks) / len(risks)) / params.stop_atr_k


def _member_euro(member: GroupMember, pct: float) -> float:
    """Euro P&L of ``member`` closed at its live price, shaved by ``pct``."""
    euro = (
        member.sign * (member.current_price - member.level_open) * member.euro_per_point
    )
    return euro - abs(euro) * max(pct, 0.0)


def explain_basket(
    members: list[GroupMember],
    *,
    now: datetime,
    banked_euro: float = 0.0,
    params: BasketParams | None = None,
) -> GroupPlanReport:
    """Decide, for the whole book, whether every position closes now.

    Pure function — see the module docstring for the rule. ``banked_euro`` is
    what the day has already realised (``DayBook.realized_euro``); it is part of
    the sum so a leg stopped out earlier is still counted against the others.

    The report never carries a stop plan (``plan`` is empty): the decision is
    :attr:`~src.exit.zones.smartgroup.GroupPlanReport.close_reason`, set when the
    book closes, :attr:`~src.exit.zones.smartgroup.GroupPlanReport.review_due` and
    :attr:`~src.exit.zones.smartgroup.GroupPlanReport.close_alert_due`.
    """
    params = params or BasketParams()
    local = now.astimezone(PARIS).time()

    total = banked_euro
    valuations: list[MemberValuation] = []
    unpriceable: list[int] = []
    for m in members:
        if m.euro_per_point <= 0 or m.level_open <= 0:
            unpriceable.append(m.position_id)
            continue
        euro = _member_euro(m, params.reconcile_margin_pct)
        total += euro
        valuations.append(
            MemberValuation(
                position_id=m.position_id,
                candidate=m.current_price,
                level_follower=m.level_follower,
                tightens=False,
                valued_at=m.current_price,
                euro=euro,
            )
        )

    target = basket_target_euro(members, params)
    gate = target if target is not None else 0.0
    armed = target is not None and not unpriceable and total > target
    return GroupPlanReport(
        plan={},
        total_euro=total,
        gate_euro=gate,
        armed=armed,
        valuations=valuations,
        unpriceable=unpriceable,
        close_reason=BASKET_WIN_REASON if armed else None,
        review_due=not armed and local >= params.review_at,
        close_alert_due=not armed and local >= params.alert_at,
        close_only=True,
    )


@dataclass
class BasketCloseStop(StopUpdater):
    """Zone updater that holds every stop and lets the book decide the exit.

    Selected in zone 1 (``CLOSE_ZONESTART=basket``) it makes the profile
    group-aware: the whole-book close runs in every zone. It is also registered
    for the profit zone (``CLOSE_ZONEPROFIT=basket``) as a plain hold, so a leg
    running far into profit is not trailed out of the basket on its own.
    """

    name = "basket"

    params: BasketParams = BasketParams()

    def explain(
        self,
        members: list[GroupMember],
        *,
        now: datetime | None = None,
        banked_euro: float = 0.0,
    ) -> GroupPlanReport:
        """The whole-book decision for this tick (delegates to the pure fn)."""
        return explain_basket(
            members,
            now=now or datetime.now(UTC),
            banked_euro=banked_euro,
            params=self.params,
        )

    def propose(self, ctx: StopContext) -> float | None:
        # The stop never moves: the far initial stop is a catastrophe stop, and
        # the exit is the book's decision (see :func:`explain_basket`).
        return None
