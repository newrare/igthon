"""Zone 1 updater — ``timedlift``: periodic, noise-safe re-computation of the stop.

Selected by ``CLOSE_ZONESTART``. While the bid is still at or below break-even the
default behaviour is to freeze the stop posted at open
(:class:`~src.exit.zones.underwater.UnderwaterStop`) for the whole life of the
excursion, whatever the market does in between. That is safe but blind: a trade
that opened on a spike and then built a solid floor twenty points higher keeps
risking the full initial distance, even though the market has since shown where
it is actually defended.

``timedlift`` re-reads that floor **on a fixed cadence** instead of on every tick:

- for the first :attr:`period_minutes` after the open the stop posted at open is
  left strictly untouched — the trade needs room to breathe before anything is
  read into its path;
- from then on, once per period, it looks at the close-out price's evolution over
  the **last completed period** and asks a single question: has the market built a
  floor solid enough that the stop can be moved in behind it? If yes the stop
  tightens; if not it stays exactly where it is. The stop is **never loosened** —
  it is tighten-only by construction (a proposal short of the current follower is
  dropped).

Everything the updater proposes stays **short of break-even**: this zone's job is
to reduce the risk still being carried, not to lock a profit. Locking at or past
break-even belongs to the margin zone (``CLOSE_ZONEMARGE``), which takes over as
soon as price clears break-even.

The rule is direction-free — for a SELL the "floor" is the period's highest offer
and the stop is moved *down* onto it — because every comparison goes through the
direction-aware helpers on :class:`~src.exit.zones.base.StopContext`.

Two independent distances keep the tightened stop away from ordinary market noise
— the whole point of the updater is that a *tighter* stop must not become a
*fragile* stop:

- the **cushion** sits between the period's support (its worst close-out print)
  and the proposed stop, so the stop is placed behind a level the market has
  already defended rather than on it;
- the **safety clearance** is the minimum gap the proposal must keep from the
  **live price**. It is sized on the epic's own jitter (adverse tick noise), its
  ATR, its spread and IG's minimum stop distance, so a stop is never posted where
  an ordinary pull-back — or the broker's own floor — would reach it. When the
  candidate does not clear it, the updater **holds** rather than pulling the level
  back to fit: never getting closer to price than the epic's noise allows takes
  precedence over tightening this period.

Because the review window is quantised to period boundaries (and not a window
sliding with every tick), the proposed level is *constant* for the whole period.
The stop therefore moves at most once per period, which is what keeps this from
degenerating into a per-tick trailing stop under water.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from src.exit.zones.base import StopContext, StopUpdater
from src.feed.price_buffer import Candle


@dataclass
class UnderwaterTimedLiftStop(StopUpdater):
    """Recompute the under-water stop once per period from the recent bid floor."""

    name = "timedlift"

    #: Review cadence, in minutes. Also the initial grace period: nothing is
    #: proposed before one full period has elapsed since the open, so the stop
    #: chosen at open governs the trade's first minutes untouched.
    period_minutes: float = 10.0

    #: Adverse-tick-noise band (same measure as the profit trailing floor): the
    #: per-epic amplitude of an ordinary down-tick, used to size both distances
    #: below rather than any fixed offset.
    noise_window: int = 20
    noise_std_k: float = 2.0

    #: Cushion kept between the period's support and the proposed stop, as a
    #: multiple of the noise band / of ATR. The widest of the two (and of one
    #: spread) wins, so the stop sits *under* the defended level, not on it.
    cushion_noise_mult: float = 1.0
    cushion_atr_k: float = 0.5

    #: Minimum clearance the proposal must keep below the **live bid**, as a
    #: multiple of the noise band / of ATR / of the spread. Deliberately wider
    #: than the cushion: the stop may sit under an old support only as long as it
    #: is also far enough from where price trades *now*. IG's minimum stop
    #: distance is folded in so the proposal is one the broker can actually hold.
    safety_noise_mult: float = 2.0
    safety_atr_k: float = 1.0
    safety_spread_k: float = 2.0

    #: Minimum advance (× ATR) over the current follower before a new level is
    #: worth posting — stops a sequence of periods from re-pushing the same stop
    #: a fraction of a point higher each time.
    min_advance_atr_k: float = 0.25

    def _review_window(self, candles: list[Candle]) -> list[Candle]:
        """Candles of the **last completed period**, or ``[]`` while none has elapsed.

        ``candles`` is the position's own history (the buffer the close profile
        bounds to the open), so the first candle's timestamp is the open reference
        and the last one is *now*. The window is quantised to period boundaries
        counted from that open: during period *n* the updater always reviews the
        exact same closed slice ``[open + (n−1)·T, open + n·T)``, so its proposal
        does not drift with every incoming tick — the stop is reviewed once per
        period, not continuously.
        """
        if len(candles) < 2 or self.period_minutes <= 0:
            return []
        opened_at = candles[0].timestamp
        period = timedelta(minutes=self.period_minutes)
        elapsed = candles[-1].timestamp - opened_at
        completed = int(elapsed / period)
        if completed < 1:
            return []
        window_end = opened_at + completed * period
        window_start = window_end - period
        return [c for c in candles if window_start <= c.timestamp < window_end]

    def propose(self, ctx: StopContext) -> float | None:
        # A follower at or past break-even means a prior excursion already locked
        # a level there: this zone has nothing left to tighten (the margin zone's
        # lock and its own backstop govern from there).
        if ctx.level_follower <= 0 or ctx.gain(ctx.level_follower) >= 0:
            return None

        window = self._review_window(list(ctx.buf.candles))
        if not window:
            return None

        noise = ctx.adverse_noise(self.noise_window, self.noise_std_k)

        # Support of the period: the worst close-out price actually printed over it
        # (the lowest bid low for a BUY, the highest offer high for a SELL). Wicks,
        # not closes, are used on purpose — the stop must survive what the market
        # really traded through, not only the minute closes.
        support = min((ctx.bar_adverse(c) for c in window), key=ctx.gain)
        cushion = max(
            self.cushion_noise_mult * noise,
            self.cushion_atr_k * ctx.atr_value,
            ctx.spread,
        )
        candidate = ctx.offset(support, -cushion)

        # Stay strictly short of break-even: this zone reduces the risk carried, it
        # never locks a profit (that is CLOSE_ZONEMARGE's job, which takes over as
        # soon as price clears break-even).
        if ctx.gain(candidate) >= 0:
            return None

        # Tighten-only, with a minimum advance so a period that barely improves the
        # level does not cost a broker push (and a stop-history point) for nothing.
        advance = ctx.sign * (candidate - ctx.level_follower)
        if advance <= self.min_advance_atr_k * ctx.atr_value:
            return None

        # Safety clearance: never approach the live price closer than the epic's own
        # noise, ATR, spread and IG's minimum stop distance allow. Note this HOLDS
        # rather than clamping the candidate back to the limit — a period whose
        # floor is too close to where price trades now simply does not move the
        # stop, and the previous (safer) follower keeps protecting the trade.
        safety = max(
            self.safety_noise_mult * noise,
            self.safety_atr_k * ctx.atr_value,
            self.safety_spread_k * ctx.spread,
            ctx.min_stop_distance,
        )
        if ctx.sign * (ctx.current_price - candidate) < safety:
            return None

        return candidate
