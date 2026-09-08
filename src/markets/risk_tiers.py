"""Risk-tier epic selection — which 40 of the ~140 discovered epics we watch.

IG caps a Lightstreamer connection at a fixed number of subscriptions, so the
full epic list discovered by :class:`~src.markets.market_scanner.MarketScanner`
(around 140 markets) must be narrowed to the subset the bot actually watches and
therefore the only subset it can ever open. That choice used to be a pure
diversification pick (the tightest spread of each asset class, round-robin).
This module replaces *what* gets picked with a **profit-funded risk tier**.

The idea, in one line: **start the day on the small markets and only move up to
the bigger ones with money the day has already banked.**

Each market carries an estimated euro cost of being wrong —
``MarketInfo.stop_loss_eur``, what a minimum-size BUY loses when it is stopped
out at IG's minimum stop distance. The tiers slice that cost::

    tier 0 :     0 € < risk <=  30 €     unlocked from the first minute
    tier 1 :    30 € < risk <= 100 €     unlocked once  30 € are realised
    tier 2 :   100 € < risk <= 200 €     unlocked once 100 € are realised
    tier 3 :   200 € < risk <= 600 €     unlocked once 200 € are realised

The unlocked tier is **derived on every pass** from the day's *realised* P&L
(closed positions only — an unrealised gain is not a gain), never latched: a
give-back walks the ladder straight back down and the watched list returns to the
small markets. The day therefore starts flat on tier 0 and can only escalate with
the market's money, which is the same invariant
:mod:`src.entry.open_progressive` enforces one level lower, on the single
position it sizes. Here the ladder is applied one level *up*, on the universe
itself, so it constrains every entry strategy — not only the ones that opt into a
risk ladder.

Two rules keep the list usable rather than merely pure:

* **An epic holding an open position is always kept**, whatever tier it belongs
  to. Dropping it would cut the live feed the position monitor and its chart
  read, so the pins come first and the tier fills what is left.
* **The list is always filled to the cap.** A tier is a preference, not a quota:
  when the band holds fewer markets than there are slots, the remainder is taken
  first from *below* the floor (cheaper markets can never break the invariant),
  then from above the ceiling, and only last from the markets whose risk could
  not be estimated. Watching 12 markets because only 12 happen to be priced in
  the 100-200 € slice would forfeit the afternoon for nothing.

Within every pool the pick stays diversified across asset classes (indices /
forex / commodities) round-robin, exactly as before, so a tier is never filled
end to end with FX pairs.

Note on the figure used: ``stop_loss_eur`` prices IG's *minimum* stop distance,
not the stop the selected ``STOP_STRATEGY`` will actually choose — it is the
floor of what an epic can cost, and it is the same number the Epic List page
shows in its risk column. It is a per-market property (contract size, price,
stop rule), which is exactly what a *universe* filter needs: it must be
computable before any position or curve exists.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from src.markets.market_scanner import MarketInfo

logger = logging.getLogger(__name__)

# Euro boundaries of the ladder, ascending: ``bounds[i]`` is tier *i*'s floor and
# ``bounds[i + 1]`` its ceiling, so N bounds describe N - 1 tiers. A tier is
# unlocked once the day's realised P&L is strictly above its floor, which makes
# the first tier (floor 0) the one a flat or losing day stays on.
DEFAULT_TIER_BOUNDS: tuple[float, ...] = (0.0, 30.0, 100.0, 200.0, 600.0)


@dataclass(frozen=True)
class RiskTier:
    """One rung of the ladder: the euro band epics must price into."""

    index: int
    floor: float
    ceiling: float

    def contains(self, risk: float) -> bool:
        """True when ``risk`` is inside the band (floor exclusive, ceiling included)."""
        return self.floor < risk <= self.ceiling

    @property
    def label(self) -> str:
        """Compact ``30-100€`` label used in logs and Epic List reasons."""
        return f"{self.floor:.0f}-{self.ceiling:.0f}€"


@dataclass
class TierSelection:
    """Result of a selection pass — the picked markets plus why they were picked."""

    markets: list[MarketInfo] = field(default_factory=list)
    tier: RiskTier = RiskTier(0, 0.0, 0.0)
    pinned: int = 0
    from_band: int = 0
    from_cheaper: int = 0
    from_dearer: int = 0
    from_unknown: int = 0

    @property
    def epics(self) -> list[str]:
        """Epics of the selected markets, in selection order (pins first)."""
        return [m.epic for m in self.markets]

    def summary(self) -> str:
        """One-line breakdown of where the slots went, for the refresh log."""
        return (
            f"tier {self.tier.index} ({self.tier.label}): {len(self.markets)} epic(s) "
            f"— {self.pinned} open, {self.from_band} in band, "
            f"{self.from_cheaper} cheaper, {self.from_dearer} dearer, "
            f"{self.from_unknown} unpriced"
        )


def tier_for_pnl(
    realised_today: float,
    bounds: Sequence[float] = DEFAULT_TIER_BOUNDS,
) -> RiskTier:
    """The tier the day's banked P&L has unlocked.

    A tier is unlocked when ``realised_today`` is strictly above its floor, so a
    flat day (0 €) and a losing day both sit on the first tier. Recomputed from
    the current P&L on every pass, never latched — losing back the gains drops
    the universe to the small markets again.
    """
    if len(bounds) < 2:
        raise ValueError("tier bounds need at least a floor and a ceiling")
    index = 0
    for candidate in range(1, len(bounds) - 1):
        if realised_today > bounds[candidate]:
            index = candidate
    return RiskTier(index=index, floor=bounds[index], ceiling=bounds[index + 1])


def _round_robin(
    markets: list[MarketInfo],
    cap: int,
    *,
    key,
) -> list[MarketInfo]:
    """Pick at most ``cap`` markets balanced across asset classes.

    Groups by ``instrument_type`` and takes one market per class per round, each
    class ordered by ``key``. Classes that run out drop off the rotation and
    their slots go to the classes that still have markets, so the result keeps
    the best market of every class instead of filling every slot with the one
    class that happens to score best globally (FX spreads, typically).
    """
    if cap <= 0 or not markets:
        return []
    if len(markets) <= cap:
        return sorted(markets, key=key)

    buckets: dict[str, list[MarketInfo]] = {}
    for market in markets:
        buckets.setdefault(market.instrument_type.upper() or "OTHER", []).append(market)
    for bucket in buckets.values():
        bucket.sort(key=key)

    # Stable class order keeps the selection deterministic across refreshes.
    order = sorted(buckets)
    cursors = dict.fromkeys(order, 0)
    chosen: list[MarketInfo] = []
    while len(chosen) < cap:
        progressed = False
        for class_name in order:
            if len(chosen) >= cap:
                break
            cursor = cursors[class_name]
            if cursor < len(buckets[class_name]):
                chosen.append(buckets[class_name][cursor])
                cursors[class_name] += 1
                progressed = True
        if not progressed:
            break
    return chosen


def select_by_risk_tier(
    markets: Sequence[MarketInfo],
    *,
    cap: int,
    tier: RiskTier,
    pinned: Iterable[str] = (),
) -> TierSelection:
    """Pick the ``cap`` markets to watch for ``tier``, pinning the open positions.

    Slots are handed out in this order, each pool diversified across asset
    classes and cut short as soon as the cap is reached:

    1. **pins** — every epic in ``pinned`` that is still in ``markets``. A live
       position must keep its feed whatever tier its market belongs to.
    2. **the band** — ``floor < risk <= ceiling``, the markets the day has earned
       the right to, tightest spread first within each class.
    3. **cheaper** — ``risk <= floor``, closest to the floor first. Always safe:
       a smaller stop-out cannot break the ladder's invariant.
    4. **dearer** — ``risk > ceiling``, cheapest first. Only reached when
       everything at or below the tier is exhausted.
    5. **unpriced** — markets whose ``stop_loss_eur`` is unknown, last, because
       an unknown cost is precisely what the ladder cannot bound.

    ``cap <= 0`` disables the cap and returns every market, pins first.
    """
    pinned_set = set(pinned)
    ordered_pins = [m for m in markets if m.epic in pinned_set]
    rest = [m for m in markets if m.epic not in pinned_set]

    if cap <= 0:
        return TierSelection(
            markets=[*ordered_pins, *rest],
            tier=tier,
            pinned=len(ordered_pins),
            from_band=len(rest),
        )

    # Pins take their slots first and are never dropped, even when there are more
    # open positions than the cap allows (the cap is then honoured by the pins
    # alone — the feed cannot subscribe more, and a live position outranks a
    # candidate every time).
    chosen = list(ordered_pins)

    band: list[MarketInfo] = []
    cheaper: list[MarketInfo] = []
    dearer: list[MarketInfo] = []
    unpriced: list[MarketInfo] = []
    for market in rest:
        risk = market.stop_loss_eur
        if risk is None:
            unpriced.append(market)
        elif tier.contains(float(risk)):
            band.append(market)
        elif float(risk) <= tier.floor:
            cheaper.append(market)
        else:
            dearer.append(market)

    def by_spread(market: MarketInfo) -> float:
        return market.spread_ratio

    # Cheaper markets are taken from the floor downwards and dearer ones from the
    # ceiling upwards, so a fill always lands as close to the unlocked band as the
    # universe allows.
    def by_risk_desc(market: MarketInfo) -> tuple[float, float]:
        return (-float(market.stop_loss_eur or 0.0), market.spread_ratio)

    def by_risk_asc(market: MarketInfo) -> tuple[float, float]:
        return (float(market.stop_loss_eur or 0.0), market.spread_ratio)

    counts: list[int] = []
    for pool, key in (
        (band, by_spread),
        (cheaper, by_risk_desc),
        (dearer, by_risk_asc),
        (unpriced, by_spread),
    ):
        room = cap - len(chosen)
        picked = _round_robin(pool, room, key=key) if room > 0 else []
        chosen.extend(picked)
        counts.append(len(picked))

    return TierSelection(
        markets=chosen,
        tier=tier,
        pinned=len(ordered_pins),
        from_band=counts[0],
        from_cheaper=counts[1],
        from_dearer=counts[2],
        from_unknown=counts[3],
    )
