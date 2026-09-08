"""``adaptive`` — one reading-driven updater for zones 1, 2 and 3.

Every other updater in :mod:`src.exit.zones` applies **the same rule to every
epic**: a fraction of the risk here, a swing low there, a fixed confirmation
window everywhere. That is deliberate for a stop *placement* rule, but it answers
the wrong question once a position is open. The question then is not "where should
the stop sit?" — it is *"is the market still doing what this trade was opened
for?"*, and the answer is different on every epic and on every hour of the day.

This updater asks that question instead, from the shared reading facade
(:mod:`src.core.reading` — the same measures the dashboard's *read this market*
view shows), through :meth:`~src.exit.zones.base.StopContext.reading`. It reads
the curve **since the open**, in the position's own profit terms, classifies it
into one of a handful of readable situations (:class:`Situation`) and then decides
what that situation is worth **in the zone the price currently sits in**:

=============  ==========================  ================================
Situation      Zone 1 (under break-even)   Zones 2 & 3 (in profit)
=============  ==========================  ================================
``TREND``      hold                        hold — let the trailing take over
``FADING``     hold — wait for break-even  close: take what is on the table
``FLAT``       close **if today can        close: the gain will not grow
               afford the loss**
``REVERSAL``   close now — the trade was   close now
               wrong about the direction
=============  ==========================  ================================

The zone-1 reversal is additionally held back while the background trend still
points the trade's way (see below).

Three of those cells are what makes this profile different from a stop rule:

- **Flat under water is priced, not guessed.** A position that has gone nowhere
  for a quarter of an hour is not going to be saved by a wider stop, but closing
  it costs real money *now*. So the cost of closing is charged to a **budget for
  the whole day** (:class:`~src.exit.zones.base.DayBook`): a fixed share of what
  the day banked *elsewhere*, minus what the cuts already spent. A day in the
  green can free a stagnating position and move on; a day with nothing banked, or
  one that has spent its budget, keeps the position and lets the protective stop
  do its job. The budget is cumulative and cannot be eroded by its own spending —
  the first version compared each cut against the day's *net*, which every cut
  lowered, so the series ate the whole banked gain (five cuts, −51.52 €, against
  the +38.60 € standing when the first fired: live, 2026-08-28). This is the one
  input a per-tick exit rule cannot derive from the curve, which is why it is
  threaded all the way down from the monitor pass.
- **A visible reversal is closed before the stop, not at it.** Once the move since
  open is a clean adverse run of at least an ATR, the trade was wrong about the
  direction: riding it to the stop only pays the rest of the risk to learn the same
  thing. The close is recorded as ``reversal`` so it is distinguishable in the
  history from a stop-out.
- **No reversal is booked against the background trend.** A fifteen-candle window
  cannot tell "the move turned" from "the move is breathing"; the hour behind it
  can. So the under-water reversal is refused while the hour-long curve is still a
  clean line the trade's way
  (:meth:`AdaptiveReadStop._market_still_favourable`) — the only measure here read
  on the market rather than since the open. The flat cut is *not* guarded that way:
  measured on the archive, holding stagnating positions against a rising background
  only walks them into their stop.
- **A tiring rise is banked, but a noisy one is not.** "The market cleared
  break-even and is now dithering" is exactly the state a trailing stop handles
  badly (its stop is still behind the noise band) and a reading handles well: the
  progression no longer clears the band the curve wanders in, or has lost its
  speed, so the gain is taken rather than given back. What makes that reading
  usable is the unit it is measured in — a **band**, not a line. The first version
  called a favourable window "fading" when its regression fit badly (R² below
  :attr:`~AdaptiveReadStop.clean_r2`), which confuses "the move is over" with "the
  move is jagged": a rise worth several ATR still fits a line badly if the path
  inside it zigzags, and a zigzag whose amplitude *is* the epic's own noise is
  noise. So the continuation is judged on
  :attr:`~AdaptiveReadStop.trend_band_k` multiples of the window's band thickness
  instead, and the line test stays where a *before and after* is genuinely being
  claimed: the reversal.

This updater **never moves a stop** — :meth:`AdaptiveReadStop.propose` always
returns ``None``. It is a hold-or-close layer, so it composes cleanly with the
zones it is *not* selected in: the profit zone (``CLOSE_ZONEPROFIT``) keeps
trailing exactly as configured, and the protective stop placed at open
(``STOP_STRATEGY``) stays the floor under everything.

Selected per zone in ``.env``, as always::

    CLOSE_ZONESTART=adaptive
    CLOSE_ZONEMARGE=adaptive
    CLOSE_ZONESECURE=adaptive

Everything is direction-free: the reading is expressed in profit terms (rising
always means "going our way", whichever side the position is on), so a SELL is
managed by the same rules mirrored.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from src.core.reading import PositionReading, Trend
from src.exit.zones.base import (
    StopContext,
    StopUpdater,
    StopZone,
    ZoneDecision,
    classify_zone,
)

#: Close reasons this updater can emit, persisted as ``Position.reason_close``.
#: Kept as module constants so the dashboard labels and the tests name the same
#: strings the updater does.
REASON_REVERSAL = "reversal"
REASON_FLAT_CUT = "flat_cut"
REASON_FLAT_BREAKEVEN = "flat_breakeven"
REASON_FLAT_PROFIT = "flat_profit"


class Situation(Enum):
    """What the curve has been doing to the position over the recent window.

    Read in **profit terms**: "rising" always means moving the trade's way, so the
    same four situations describe a long and a short.
    """

    #: Not enough history yet (or no volatility measure) to say anything honest.
    UNREADABLE = "unreadable"
    #: The move the trade was opened for is still happening, cleanly.
    TREND = "trend"
    #: Still going our way, but measurably slower or messier than it was.
    FADING = "fading"
    #: Going nowhere: the window's net move is inside its own noise.
    FLAT = "flat"
    #: A clean, sizeable move **against** the position — the trend turned.
    REVERSAL = "reversal"


@dataclass
class AdaptiveReadStop(StopUpdater):
    """Hold or close from a reading of the market since the position opened.

    See the module docstring for the decision table. The thresholds below are the
    only tuning surface; all of them are expressed as ratios or in ATR units, so
    they mean the same thing on a forex pair and on an index.
    """

    name = "adaptive"

    #: Window, in candles, the situation is read over — "flat/turned for the last
    #: 10 or 20 minutes". Clamped to the position's own age.
    window: int = 15
    #: Nothing is decided before the position has this many candles of history: a
    #: trade that opened four minutes ago has no readable curve of its own, and the
    #: protective stop is the only thing that should govern it.
    min_minutes_open: int = 10
    #: R² above which a fit counts as a real line rather than a wander. Used to
    #: confirm a **reversal** — the one decision here that claims a before and an
    #: after — and to read the background trend
    #: (:meth:`AdaptiveReadStop._market_still_favourable`). A favourable
    #: continuation is *not* judged on it: see :attr:`trend_band_k`.
    clean_r2: float = 0.5
    #: How big the adverse move over the window must be, in ATR units, before it is
    #: called a reversal rather than a pull-back inside the move.
    reversal_atr_k: float = 1.5
    #: Candles a position must have been open before a reversal can be read at all.
    #: A reversal is a *before* and an *after*; on a window as long as the position's
    #: whole life the two are the same thing, and "the trend turned since the open"
    #: becomes indistinguishable from "the trade opened badly". Measured live on
    #: 2026-08-28: eight of the fifteen reversal cuts fired at 11-13 minutes, the
    #: earliest the rule then allowed.
    reversal_min_minutes: int = 20
    #: Favourable excursion, in **spreads**, the position must have shown before the
    #: turn — the same "something to reverse" bar as :attr:`reversal_mfe_atr_k`, read
    #: in the other unit that matters. On an epic whose spread is most of a candle
    #: range (USD/JPY quoted 0.010 wide on a 0.012 ATR: 0.83 ATR), one ATR of MFE is
    #: 1.2 round trips — a wick, not a gain. The two floors are combined with
    #: ``max``, so the stricter one always wins. Measured on position 926
    #: (2026-09-01): cut for a "reversal" on an MFE of 1.5 pip (1.25 ATR, +1.40 €
    #: at best, 4 % of its life in profit), for −30.43 €, on a curve that then ran
    #: 10 pips past break-even without ever coming near its stop.
    reversal_mfe_spread_k: float = 2.0
    #: Favourable excursion, in ATR units, the position must have shown **before**
    #: the turn — the "something" a reversal reverses. Without it the rule fires on
    #: a trade that has simply never worked, which is not a reversal but the
    #: ordinary drawdown the protective stop is sized for. Same day: eleven of the
    #: fifteen cuts were taken on positions that had never once been in profit, and
    #: closing them cost 718.40 € where holding them to their stop was worth
    #: +250.71 € (see docs/strategies/close_adaptive.md).
    reversal_mfe_atr_k: float = 1.0
    #: Candles since the best moment before the turn counts as established rather
    #: than as one bad print at the top.
    reversal_turn_minutes: int = 5
    #: A continuation is "fading" once the window's speed drops below this share of
    #: the average speed since the open.
    fade_ratio: float = 0.4
    #: Multiples of the window's **band thickness** the net favourable move must
    #: clear for the continuation to still count as the move the trade was opened
    #: for. This is the band test that replaced an R² test on the same branch: a
    #: rise can be worth several ATR and still fit a line badly, because the path
    #: inside it zigzags — and a zigzag whose amplitude is the epic's own noise is
    #: noise, not a fade. Measured live on position 929 (EUR/NZD BUY, 2026-09-02):
    #: closed ``flat_profit`` at 11 minutes on +2.76 ATR of progress because the
    #: 11-candle fit read R² 0.22, where the same window cleared its band 3.6 times
    #: over. See docs/strategies/close_adaptive.md.
    trend_band_k: float = 2.0
    #: Share of the day's banked result **all** the under-water flat cuts of the
    #: day may consume, together (see
    #: :meth:`~src.exit.zones.base.DayBook.flat_cut_budget`). A day that banked
    #: +100 € elsewhere can spend 20 € on cutting stagnating positions loose; the
    #: 21st euro waits for the next winner.
    flat_cost_ratio: float = 0.20
    #: Candles a position must have been open before an under-water flat cut is
    #: even considered — the *upper* end of the "flat for 10 or 20 minutes" range,
    #: deliberately longer than :attr:`min_minutes_open`. A cut books a loss, so it
    #: must not fire on the first window that happens to read flat: measured live
    #: on 2026-08-28, three of the five cuts fired at 12-15 minutes of age, one of
    #: them on a position that had been +10.50 € eight minutes earlier. Reading a
    #: flat *in profit* keeps the shorter minimum — banking a gain costs nothing.
    flat_cut_min_minutes: int = 20
    #: Horizon, in candles, the **background** trend is read over before zone 1 is
    #: allowed to book a loss (see :meth:`_market_still_favourable`). Read on the
    #: unbounded market curve, not the since-open one: it is the only measure in
    #: this updater that looks further back than the position itself.
    context_minutes: int = 60

    # ---- the decision -----------------------------------------------------------

    def propose(self, ctx: StopContext) -> float | None:
        """Never moves the stop — this updater only holds or closes (see
        :meth:`decide`)."""
        return None

    def decide(self, ctx: StopContext) -> ZoneDecision:
        """Hold, or close with the reason the reading justifies."""
        reading = ctx.reading()
        situation = self.read_situation(reading)
        if situation in (Situation.UNREADABLE, Situation.TREND):
            # Nothing readable yet, or the trade is doing exactly what it was
            # opened for: in both cases the position is left alone.
            return ZoneDecision()

        zone = self._zone(ctx)
        if zone is StopZone.UNDERWATER:
            return self._decide_underwater(ctx, reading, situation)
        return self._decide_in_profit(zone, situation)

    def _decide_underwater(
        self, ctx: StopContext, reading: PositionReading, situation: Situation
    ) -> ZoneDecision:
        """Zone 1 — closing costs money, so only two situations justify it.

        A **reversal** is closed outright: the market walked away from the level
        the trade was built on, and the only thing riding it to the stop buys is
        the rest of the planned risk.

        A **flat** market is closed only when it has been flat long enough to be
        believed *and* the day can still afford it (see
        :meth:`_flat_cut_affordable`) — that is the whole point of the rule: a
        stagnating position is a cost of opportunity, not a threat, and freeing it
        must never turn a green day red.

        A **fading** move is held: price is still coming our way and break-even is
        ahead of it, so the position is given the chance to reach it — zone 2 then
        takes the no-loss exit if the fade persists.

        The **reversal** — and only the reversal — is additionally refused while the
        background trend still points the trade's way
        (:meth:`_market_still_favourable`): a pull-back inside a standing move is
        exactly what a fifteen-candle window mistakes for a turn. The flat cut is
        deliberately **not** guarded that way, and the archive says why: guarding it
        too keeps stagnating positions alive against a rising background until they
        reach their stop instead (54 cuts become 39 and 31 stop-outs become 38, for
        −1228 € over the ten archived weeks).
        """
        if situation is Situation.REVERSAL:
            if self._market_still_favourable(reading):
                return ZoneDecision()
            return ZoneDecision(close_reason=REASON_REVERSAL)
        if (
            situation is Situation.FLAT
            and reading.minutes_open >= self.flat_cut_min_minutes
            and self._flat_cut_affordable(ctx, reading)
        ):
            return ZoneDecision(close_reason=REASON_FLAT_CUT)
        return ZoneDecision()

    def _market_still_favourable(self, reading: PositionReading) -> bool:
        """Is the hour-long curve still climbing the position's way, cleanly?

        The one measure in this updater taken on the **unbounded** market curve
        (:attr:`~src.core.reading.PositionReading.market`) rather than since the
        open. Everything else here reads a window of at most 15 candles, which
        cannot distinguish "the move turned" from "the move is breathing": the
        two look identical over a quarter of an hour, and only the hour behind them
        tells them apart.

        While the long horizon is a clean line in the trade's favour, no reversal is
        booked — the protective stop keeps the floor and the position is given the
        chance the background trend says it has. Measured on position 926
        (USD/JPY BUY, 2026-09-01): cut as a reversal at 10:26 on a 15-minute fit of
        −1.75 ATR while ``trend_60m`` read +0.047 % at R² 0.53 and price sat at
        47 % of its own 60-minute channel. The curve then climbed 10 pips past
        break-even; the cut paid 53 % of the planned risk to learn nothing.

        A buffer shorter than :attr:`context_minutes` has no background to read, so
        the guard stays out of the way (it can only ever *prevent* a close, and a
        guard that fires on a fit as short as the position's own life would prevent
        every one of them).
        """
        market = reading.market
        if not market.is_warm(self.context_minutes):
            return False
        trend = market.trend(self.context_minutes)
        if trend.r_squared < self.clean_r2:
            return False
        favourable = Trend.UP if reading.sign > 0 else Trend.DOWN
        return trend.direction is favourable

    @staticmethod
    def _decide_in_profit(zone: StopZone, situation: Situation) -> ZoneDecision:
        """Zones 2 and 3 — the gain is real, so anything but a live run banks it.

        The two zones differ only in what is being banked, and therefore in how the
        exit is recorded: in the break-even band the gain is barely more than the
        round trip (``flat_breakeven`` — the "at least no loss" exit the plan hoped
        for), past the margin line it is money clear of the epic's own noise
        (``flat_profit``). A reversal is recorded as such in both.
        """
        if situation is Situation.REVERSAL:
            return ZoneDecision(close_reason=REASON_REVERSAL)
        reason = (
            REASON_FLAT_BREAKEVEN
            if zone is StopZone.BREAKEVEN_BAND
            else REASON_FLAT_PROFIT
        )
        return ZoneDecision(close_reason=reason)

    def _flat_cut_affordable(self, ctx: StopContext, reading: PositionReading) -> bool:
        """Can the day's remaining flat-cut budget absorb closing this position?

        The comparison the strategy is built on: what the exit **costs** against
        what the day can tolerate losing on this rule. The budget is a fixed share
        of the day's result *excluding* the flat cuts, minus what the cuts have
        already spent today — a **cumulative** cap
        (:meth:`~src.exit.zones.base.DayBook.flat_cut_budget`), so a run of
        individually-affordable cuts can never add up to a day-sized loss the way a
        per-decision test allows.

        A position that cannot be priced (no ``€/point``) is never closed on this
        rule — a missing input must not read as "free".
        """
        if ctx.euro_per_point <= 0:
            return False
        cost = -reading.gain_euro
        if cost <= 0:
            # Flat exactly at (or above) break-even: the exit is free, take it.
            return True
        return cost <= ctx.day_book.flat_cut_budget(self.flat_cost_ratio)

    # ---- the reading ------------------------------------------------------------

    def read_situation(self, reading: PositionReading) -> Situation:
        """Classify the curve since the open into one :class:`Situation`.

        Pure and side-effect free, and deliberately public: it is the whole content
        of this profile, so it is what the tests (and a future dashboard "why did it
        close?" panel) read directly.

        The order of the tests is the order of certainty. A clean adverse run is a
        reversal whatever else it looks like; a window whose net move is inside its
        own noise is flat, whatever the fit says about it; only then is a favourable
        move split into a continuation worth holding and a tiring one — on the
        **band** it clears and the speed it keeps, never on how well it fits a
        line (see :attr:`trend_band_k`).
        """
        minutes = reading.minutes_open
        # Two candles is the floor every measure below needs; the configured
        # minimum is the strategy's own patience on top of it.
        if minutes < max(2, self.min_minutes_open):
            return Situation.UNREADABLE

        atr = reading.market.atr
        if atr <= 0:
            # No volatility scale: every "how big is this move?" test below would
            # silently pass or silently fail. Say nothing instead.
            return Situation.UNREADABLE

        window = min(self.window, minutes)
        closes = reading.favourable_closes
        move = closes[-1] - closes[-window]
        clean = reading.curve.trend(window).r_squared >= self.clean_r2

        # A visible turn — a move that first worked and then broke down.
        if self._is_reversal(reading, move, atr, clean):
            return Situation.REVERSAL

        # Going nowhere: the window's net move is no bigger than what a single bad
        # candle in it gives back (``is_stalling`` compares the two directly).
        if reading.is_stalling(window):
            return Situation.FLAT

        if move <= 0:
            # Adverse, but neither clean enough nor big enough to call a reversal,
            # and not flat either: an ordinary pull-back inside the trade. Held —
            # this is exactly what the protective stop is sized for.
            return Situation.UNREADABLE

        # Coming our way. Is it still the move the trade was opened for, or the
        # tail of it? Two independent ways to be "less obvious": progress that no
        # longer clears the band the curve wanders in, or progress that has kept
        # its size but lost its speed.
        band = reading.curve.amplitude(window).band_noise
        if band > 0 and move < self.trend_band_k * band:
            return Situation.FADING
        speed_now = move / window
        speed_since_open = (closes[-1] - closes[0]) / minutes
        if speed_since_open > 0 and speed_now < self.fade_ratio * speed_since_open:
            return Situation.FADING
        return Situation.TREND

    def _is_reversal(
        self, reading: PositionReading, move: float, atr: float, clean: bool
    ) -> bool:
        """Has the move the trade was opened for turned, visibly, since the open?

        Four conditions, and the first two are what separate a *reversal* from a
        trade that is merely losing:

        1. **age** — a reversal is a before and an after. Read over a window as
           long as the position's whole life, the two collapse into one and every
           badly-timed entry looks like a turn;
        2. **something to reverse** — the position must have shown a real
           favourable excursion first, measured against *both* the epic's ATR and
           its spread (:meth:`_min_reversal_mfe`). A trade that never once went our
           way is not reversing: it is inside the drawdown its protective stop was
           sized for, and cutting it early trades a small certain loss against the
           recovery it can no longer catch;
        3. **the turn is established** — the peak is at least
           ``reversal_turn_minutes`` behind, not one bad print at the top;
        4. **the run is real** — the window is a clean fit and gives back at least
           ``reversal_atr_k`` candle-ranges.

        Conditions 1 and 2 exist because of what the first version did without
        them: on 2026-08-28 it closed fifteen positions for −718.40 €, eleven of
        which had never been in profit at all, where holding the same book to its
        stops was worth +250.71 €. It won nine of those fifteen for ~23 € apiece
        and lost six for ~196 € apiece — the small-win/large-loss shape of every
        rule that truncates recoveries.
        """
        if reading.minutes_open < self.reversal_min_minutes:
            return False
        if reading.mfe < self._min_reversal_mfe(reading, atr):
            return False
        if reading.minutes_since_peak < self.reversal_turn_minutes:
            return False
        return clean and move <= -self.reversal_atr_k * atr

    def _min_reversal_mfe(self, reading: PositionReading, atr: float) -> float:
        """Smallest favourable excursion that counts as "the trade worked first".

        Two floors, and the stricter wins: ``reversal_mfe_atr_k`` candle ranges, and
        ``reversal_mfe_spread_k`` **spreads**. The ATR floor alone asks the wrong
        question on a market whose round trip is itself most of a candle range: one
        ATR of excursion is then barely more than the cost of the trade, so a wick
        one pip past the fill satisfies "it worked" and every pull-back after it
        reads as a turn.

        The spread is taken as its 30-minute mean rather than the live quote: a
        momentary widening must not be able to move a threshold this rule depends
        on.
        """
        spread = reading.market.spread_avg(30)
        return max(self.reversal_mfe_atr_k * atr, self.reversal_mfe_spread_k * spread)

    # ---- helpers ----------------------------------------------------------------

    @staticmethod
    def _zone(ctx: StopContext) -> StopZone:
        """Which zone the live close-out price sits in.

        The composer already routed the tick to this updater by zone, but the same
        updater instance is registered in three of them and the decision differs
        per zone, so it is re-derived here from the open-frozen references — the
        same derivation the composer uses (the profit trigger mirrors the margin
        line across break-even).
        """
        level_profit = ctx.level_margin + (ctx.level_margin - ctx.level_zero)
        return classify_zone(
            ctx.current_price,
            ctx.level_zero,
            ctx.level_margin,
            level_profit,
            ctx.sign,
        )
