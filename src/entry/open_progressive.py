"""Single-position ranker with a profit-funded risk ladder.

Two ideas, and they only make sense together.

**One market at a time.** ``concurrent_positions = 1`` and no wallet-bounded
fan-out: the bot holds exactly one position, re-ranks the whole tradable universe
the moment it closes, and opens the next one. Nothing is diversified away — the
day is a *sequence* of single bets, each judged on its own.

**A risk ladder funded by the day's realised gains.** Because there is only ever
one position, the size of the bet is the only lever left, and it is driven by what
the day has already **banked** (closed positions only — an open position is not a
gain). The bot starts the day allowed to risk at most ``50 €`` on a stop-out. Once
50 € are realised it stops looking at those small markets and only ranks the ones
whose stop-out costs between 50 € and 100 €; at 100 € banked it moves up to the
100–200 € band, then 200–500 €, and so on up the ladder. A loss (or a give-back)
walks the ladder straight back down — the band is *derived* from the day's P&L on
every pass, never latched.

The rung is chosen from a single allowance::

    allowance = 50 € + max(0, realised_today)
    ceiling   = the highest ladder rung <= allowance
    floor     = the rung just below it (0 on the first rung)

which enforces one invariant worth stating plainly: **a full stop-out can never
put the day below −50 €**, whatever rung it happens on, because the risk taken is
never more than the base 50 € plus what is already in the bank. That is the whole
point of scaling up on profits rather than on conviction — the escalation is paid
for by the market, not by the account.

The band is *strict* by design (``strict_band``): while it holds candidates, the
cheaper markets are not ranked at all. Escalating means escalating; a 20 € trade
taken at +120 € realised cannot move the day. Set ``strict_band = False`` to turn
the band into a plain ceiling if that preference is not wanted.

But an empty band is **not** a reason to sit out the afternoon — it only means no
epic happens to be priced in that slice right now, which says nothing about the
opportunity. So :meth:`~OpenProgressive.filter_by_risk` falls back, in order:
first **down** to the markets cheaper than the floor (always safe — a smaller
stop-out cannot break the invariant), and only if nothing at all is affordable
below the ceiling, **up** by a bounded ``band_stretch`` to catch the epics sitting
just above the line. The stretch is the one move that loosens the invariant, so it
comes last and is logged as a warning. Nothing is opened only when all three
scopes are empty.

Which market — "the surest to beat break-even fastest"
------------------------------------------------------
A position opens in the red: a BUY is filled at the offer and closes on the bid,
so the bid must climb **one whole spread** before the trade is worth anything.
That distance is what this ranker times, from the fitted trends of
:class:`~src.core.reading.CurveReading`::

    minutes_to_breakeven = spread / min(slope_5m, slope_15m)

The slower of the two fitted slopes is used on purpose — the conservative reading
of the same curve, so a 5-minute spike cannot promise a speed the 15-minute trend
does not support. Everything else in the score answers "how *sure* is that?", as a
**weighted geometric mean** (bounded above by its smallest term, so one weak
dimension collapses the whole score — no strong component rescues a fragile
market, unlike an additive sum):

- ``speed`` — ``target / (target + minutes_to_breakeven)``. Never saturates, so
  two fast markets still rank against each other.
- ``quality`` — the 15-minute regression R²: the rise is a *line*, not a wander.
- ``efficiency`` — Kaufman efficiency ratio over the context window: the path went
  somewhere instead of travelling far to stand still.
- ``pullback`` — ``1 − retracement / range``: the deepest give-back the window
  actually inflicted on a holder. A monotone climb scores ~1.
- ``alignment`` — agreement of the 5/15/30/60-minute horizons.

Hard vetoes run *first* and refuse outright, because a ranker must otherwise crown
the least-bad of the pool: the regime must be ``TREND_UP``; both the fast and slow
trends must point up; the trend must not be fading (5-minute speed opposing the
30-minute one — the earliest warning a move is dying); the spread must be worth
less than one candle (``spread_in_atr``) and not be quoted defensively
(``spread_ratio``); volatility must not be spiking against the epic's own baseline
(``volatility_ratio``); the nearest resistance must sit beyond break-even plus a
margin, else the move stalls before the trade is even alive; and break-even must
be reachable inside ``max_minutes_to_breakeven``.

What this ranker does NOT decide
--------------------------------
It stays exit-agnostic like every entry: :meth:`evaluate` emits an
:class:`~src.entry.base.EntryIntent` carrying the BUY direction and a comparable
score, nothing else. The stop that *sets* the euro risk belongs to
``STOP_STRATEGY`` and the composed :class:`~src.exit.base.CloseProfile`; this
module only reads the resulting euro figure back, through
:meth:`~src.entry.base.EntryStrategy.filter_by_risk`, to decide whether the
candidate belongs to the rung the day has paid for.

Two consequences follow, and both matter when reading a live log:

1. **The ladder cannot conjure a rung.** The euro risk of an epic is a property of
   the market (its minimum stop distance, its euro-per-point) and of the stop
   policy — not of this strategy. When the universe holds no epic priced into the
   current band, escalating means falling back (down first, then a bounded
   stretch), so the log routinely shows a trade taken outside the nominal rung.
2. **The risk figures are estimates.** They come from ``euro_per_point``, which is
   exact for a euro-quoted instrument and a reference-rate approximation for a
   foreign quote. Treat a rung boundary as a soft line, not a guarantee to the
   cent.

One limitation to keep in mind when replaying: :mod:`src.backtest.simulator`
mirrors the live rolling selector but **not the risk ladder** — it prices every
synthetic curve with a single fictional euro-per-point, so a simulated band would
be an artefact of that constant rather than a result. A simulated run reports the
*ranking* half of this strategy only.

Documented in ``docs/strategies/open_progressive.md``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from src.core.reading import CurveReading, Regime, Trend
from src.entry.base import EntryIntent, EntryStrategy
from src.feed.price_buffer import EpicBuffer

logger = logging.getLogger(__name__)

#: Floor applied to every component before the geometric mean, so a single zero
#: does not collapse every candidate to the same flat 0 and make them unrankable.
_EPS = 1e-3


def _clamp01(value: float) -> float:
    """Clamp ``value`` to the closed unit interval [0, 1]."""
    return max(0.0, min(1.0, value))


@dataclass
class OpenProgressive(EntryStrategy):
    """Rank one market at a time, and scale the risk with the day's realised gains."""

    name = "open_progressive"

    # Rolling cross-epic selection: one position, re-ranked as soon as it closes.
    cross_epic_selection = True
    concurrent_positions = 1
    wallet_bounded = False
    emits_shorts = False
    # Opt into the money-side hook (:meth:`filter_by_risk`): the scheduler prices
    # every ranked candidate in euros and reports the day's realised P&L.
    uses_risk_ladder = True
    # A tournament of three epics crowns the least-bad of three, not the best of
    # the day, and this entry acts on a single winner — so the ranking must be
    # worth something before anything is opened. Both participation gates apply:
    # strictly more than half the livestreamed universe must be warmed up AND at
    # least this many epics must be scorable, which is the gate that bites when
    # the universe itself is small (a 10-epic universe passes the ratio with 6).
    min_participation_ratio = 0.5
    min_participation_count = 20

    # ---- horizons --------------------------------------------------------------
    # The two trends timing the break-even climb. The fast one reacts, the slow one
    # confirms; the ranker always believes the slower of the two.
    fast_minutes: int = 5
    slow_minutes: int = 15
    # Window the regime, the efficiency ratio and the pull-back are judged on.
    context_minutes: int = 30
    # Candles required before an epic is scored at all. 60 keeps the whole
    # 5/15/30/60 alignment measurable rather than silently judged on fewer
    # horizons, and gives ``volatility_ratio`` a baseline worth comparing to.
    warmup_minutes: int = 60

    # ---- break-even timing -----------------------------------------------------
    # Reference time-to-break-even, in minutes: the point where the speed component
    # scores 0.5. Not a threshold — the score is continuous either side of it.
    target_minutes: float = 5.0
    # Hard veto: a market that needs longer than this to cover its own spread is
    # not "quick" by any reading and is dropped before scoring.
    max_minutes_to_breakeven: float = 20.0

    # ---- hard vetoes -----------------------------------------------------------
    # Spread worth more than this many candles: the round trip costs more than the
    # market's own movement, so break-even is a bet on an outsized move.
    max_spread_in_atr: float = 1.0
    # Live spread against its own 30-minute mean — above this the book is quoting
    # defensively (news, thin liquidity) and no price measure reveals it.
    max_spread_ratio: float = 1.5
    # Recent candle size against the epic's own baseline — above this the market is
    # spiking, and a spike is not a trend to time a break-even on.
    max_volatility_ratio: float = 1.5
    # Room to the nearest resistance, in ATR units, required *on top of* the spread:
    # a level sitting just above break-even is exactly where the climb stalls.
    min_room_atr: float = 1.0
    # Floors under the two "is this real?" components, applied as vetoes so the
    # tournament cannot crown a market that merely wanders upward.
    min_efficiency: float = 0.30
    min_r_squared: float = 0.40
    # Composite floor. 0 keeps every survivor rankable; raise it to make the ranker
    # rather stay flat than open the best of a mediocre pool.
    min_score: float = 0.0

    # ---- score weights (must sum to 1) -----------------------------------------
    weight_speed: float = 0.40
    weight_quality: float = 0.25
    weight_efficiency: float = 0.15
    weight_pullback: float = 0.10
    weight_alignment: float = 0.10

    # ---- risk ladder -----------------------------------------------------------
    # Euro rungs of the ladder, ascending. The first one is the base allowance the
    # day starts with — the most the account can ever be down on a single stop-out
    # (see the module docstring's invariant).
    risk_ladder: tuple[float, ...] = (50.0, 100.0, 200.0, 500.0)
    # True: the unlocked rung is a *band*, so the cheaper markets stop being ranked
    # once it opens (what "moving up to the 50-100 € markets" means). False turns
    # the band into a plain ceiling and keeps the small markets available.
    strict_band: bool = True
    # First fallback when the band holds nothing: take the markets BELOW its floor
    # (risk <= ceiling). Standing flat all afternoon because no epic happens to be
    # priced in the 100-200 € slice is a worse outcome than a smaller trade, and
    # trading down preserves the ladder's invariant exactly — a cheaper stop-out
    # can only cost less than the rung already paid for.
    allow_cheaper_fallback: bool = True
    # Last fallback, when nothing at all sits under the ceiling: stretch it by this
    # fraction to catch the epics sitting just above the line (0.25 = +25%, so a
    # 100 € rung reaches 125 €). Unlike the fallback above this one DOES loosen the
    # invariant — a stop-out can then put the day at ``-base - stretch x ceiling``
    # instead of ``-base`` — which is why it is tried last, kept small and logged
    # as a warning. It exists because the euro risk of a candidate is an estimate
    # to begin with (see the module docstring), so a rung boundary is a soft line;
    # 0.0 disables it and restores "stay flat rather than overpay".
    band_stretch: float = 0.25

    @property
    def warmup(self) -> int:
        return self.warmup_minutes

    @classmethod
    def from_settings(cls, settings) -> OpenProgressive:
        # Every parameter is a constant of this class (the dataclass field
        # defaults), so the strategy builds from those and ignores ``settings``.
        # Tune by editing the constants; select it with OPEN_STRATEGY.
        return cls()

    # ---- ranking ---------------------------------------------------------------

    def evaluate(self, epic: str, buf: EpicBuffer) -> EntryIntent | None:
        """Score how surely this market covers its own spread, and how fast.

        Returns ``None`` on structural grounds (too little history, no price, no
        measurable volatility) and on every hard veto — see the module docstring.
        """
        if len(buf) < self.warmup:
            return None

        reading = CurveReading.of(buf, regime_window=self.context_minutes)
        if reading.price <= 0 or reading.atr <= 0:
            return None

        # Veto 1 — the market must actually be going up, on three readings that
        # each catch what the others miss: the regime (is the path directional at
        # all?), the two fitted trends (do both horizons point up?) and the fade
        # test (is the short horizon already contradicting the long one?).
        if reading.regime is not Regime.TREND_UP:
            return None
        fast = reading.trend(self.fast_minutes)
        slow = reading.trend(self.slow_minutes)
        if fast.direction is not Trend.UP or slow.direction is not Trend.UP:
            return None
        if reading.is_fading:
            return None

        # Veto 2 — the cost of the round trip. The spread IS the break-even
        # distance for a BUY (filled at the offer, closed on the bid), so an
        # abnormal or oversized one is not a detail here: it is the whole distance
        # the trade has to travel before it is worth anything.
        spread = reading.spread
        if spread <= 0:
            return None
        if reading.spread_in_atr > self.max_spread_in_atr:
            return None
        if reading.spread_ratio > self.max_spread_ratio:
            return None
        if reading.volatility_ratio > self.max_volatility_ratio:
            return None

        # Veto 3 — how long the climb to break-even takes at the *slower* of the
        # two fitted speeds. ``slope`` is in price units per minute, so the
        # division is a time in minutes.
        speed = min(fast.slope, slow.slope)
        if speed <= 0:
            return None
        minutes_to_breakeven = spread / speed
        if minutes_to_breakeven > self.max_minutes_to_breakeven:
            return None

        # Veto 4 — headroom. A resistance sitting just above the entry is where the
        # rise stalls, and stalling before break-even is a losing trade even though
        # the curve was rising when it opened. ``room_above`` is ``inf`` when the
        # window shows no resistance at all, which passes.
        if reading.room_above < spread + self.min_room_atr * reading.atr:
            return None

        # Veto 5 — the two "is the rise real?" floors, applied before scoring so a
        # tournament cannot crown a market that merely wanders upward.
        amplitude = reading.amplitude(self.context_minutes)
        if amplitude.efficiency < self.min_efficiency:
            return None
        if slow.r_squared < self.min_r_squared:
            return None

        pullback = (
            1.0 - amplitude.retracement / amplitude.range
            if amplitude.range > 0
            else 0.0
        )
        components = (
            (
                self.target_minutes / (self.target_minutes + minutes_to_breakeven),
                self.weight_speed,
            ),
            (_clamp01(slow.r_squared), self.weight_quality),
            (_clamp01(amplitude.efficiency), self.weight_efficiency),
            (_clamp01(pullback), self.weight_pullback),
            (_clamp01(reading.alignment), self.weight_alignment),
        )
        score = 1.0
        for value, weight in components:
            score *= max(value, _EPS) ** weight
        score = _clamp01(score)
        if score < self.min_score:
            return None

        logger.debug(
            "Progressive %s: score=%.3f (break-even in %.1f min, R²=%.2f, "
            "ER=%.2f, pullback=%.2f, alignment=%.2f)",
            epic,
            score,
            minutes_to_breakeven,
            slow.r_squared,
            amplitude.efficiency,
            pullback,
            reading.alignment,
        )
        return EntryIntent(epic=epic, direction="BUY", score=score)

    # ---- risk ladder -----------------------------------------------------------

    def risk_band(self, realised_today: float) -> tuple[float, float]:
        """The euro band candidates must price into, given the day's banked P&L.

        Returns ``(floor, ceiling)``: a candidate qualifies when its estimated
        worst-case loss is ``floor < risk <= ceiling``. The floor is ``0`` on the
        first rung, and is ignored entirely when ``strict_band`` is False.

        The band is recomputed on every selection pass from the *current* realised
        P&L, so it walks back down as soon as a loss eats the day's gains — there
        is nothing latched to reset.
        """
        rungs = self.risk_ladder
        allowance = rungs[0] + max(0.0, realised_today)
        floor, ceiling = 0.0, rungs[0]
        for index, rung in enumerate(rungs):
            if rung <= allowance:
                ceiling = rung
                floor = rungs[index - 1] if index else 0.0
        return floor, ceiling

    def filter_by_risk(
        self,
        ranked: list[tuple[EntryIntent, EpicBuffer]],
        *,
        risks: Mapping[str, float | None],
        realised_today: float,
    ) -> list[tuple[EntryIntent, EpicBuffer]]:
        """Keep the candidates priced into the unlocked band — or into a fallback.

        The band is what the day has *earned the right to*, not a reason to sit
        out: an empty band means no epic happens to be priced in that slice right
        now, which says nothing about the opportunity. So three scopes are tried in
        order, and the first one that yields candidates wins:

        1. **the band** — ``floor < risk <= ceiling``, the nominal rung;
        2. **cheaper** — ``risk <= floor``, trading *down* into the smaller
           markets. Always safe: a stop-out below the rung can only cost less than
           the rung already paid for, so the ladder's invariant is untouched;
        3. **stretched** — ``ceiling < risk <= ceiling x (1 + band_stretch)``, the
           epics sitting just above the line, only when nothing at all is
           affordable below it. This one *does* loosen the invariant (a stop-out
           can reach ``-base - stretch x ceiling``), so it is last, bounded, and
           logged as a warning.

        An epic whose risk cannot be estimated is **dropped** from every scope, not
        assumed cheap: the ladder's whole guarantee is an upper bound on the loss,
        and an unknown cost is precisely what that bound cannot cover.

        Returns an empty list only when no scope holds anything — the caller then
        stays flat, and there was genuinely nothing to open.
        """
        floor, ceiling = self.risk_band(realised_today)
        priced: list[tuple[EntryIntent, EpicBuffer, float]] = []
        unknown = 0
        for intent, buf in ranked:
            risk = risks.get(intent.epic)
            if risk is None:
                unknown += 1
                continue
            priced.append((intent, buf, risk))

        def within(low: float, high: float) -> list[tuple[EntryIntent, EpicBuffer]]:
            # Ranking order is preserved: ``priced`` is still sorted by score.
            return [(i, b) for i, b, risk in priced if low < risk <= high]

        # A non-strict band is a plain ceiling, so scope 2 is already included and
        # there is nothing to fall back *down* to.
        low = floor if self.strict_band else 0.0
        kept = within(low, ceiling)
        scope = f"band {low:.0f}-{ceiling:.0f}€"

        if not kept and self.strict_band and self.allow_cheaper_fallback and floor > 0:
            kept = within(0.0, floor)
            scope = f"fallback: cheaper than {floor:.0f}€"
            if kept:
                logger.info(
                    "Progressive risk ladder: nothing priced in the %.0f-%.0f€ "
                    "band — trading down to the markets under %.0f€ (%d found)",
                    floor,
                    ceiling,
                    floor,
                    len(kept),
                )

        if not kept and self.band_stretch > 0:
            stretched = ceiling * (1.0 + self.band_stretch)
            kept = within(ceiling, stretched)
            scope = f"fallback: stretched to {stretched:.0f}€"
            if kept:
                logger.warning(
                    "Progressive risk ladder: no candidate at or below %.0f€ — "
                    "stretching the ceiling by %.0f%% to %.0f€ (%d found); a "
                    "stop-out can now cost more than the day has banked",
                    ceiling,
                    self.band_stretch * 100,
                    stretched,
                    len(kept),
                )

        logger.info(
            "Progressive risk ladder: %.2f€ realised today -> %s, %d of %d "
            "candidate(s) kept (%d with an unknown risk)",
            realised_today,
            scope,
            len(kept),
            len(ranked),
            unknown,
        )
        return kept
