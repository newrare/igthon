"""Cross-epic ranker — one basket of three unrelated markets, opened from 10:00.

The daily-basket idea: once a day, from 10:00 Paris, pick the three markets
whose morning looks the most promising, keep them as unrelated as the day
allows, open them together behind a far stop (``stop_far``) and let the
``basket`` close profile take the whole book off as soon as it is green enough
— with a review notification at 11:00 when the morning did not pay, and a
close alert at 17:30 if it is still open (the clock never closes it).

The basket is a **ranking, never a pass/fail test**: as long as three markets
can be scored, three are opened. The preferences below decide the order, not
whether the basket happens.

1. **Score the morning** — :meth:`evaluate` reads the epic's candles since
   :attr:`morning_start` and scores the move by ``ER × |move| / ATR15``: the
   Kaufman efficiency of the morning path times the size of the move since the
   morning open, in quarter-hour ATRs. A large *and* clean move ranks first; the
   side follows the move (BUY a rising morning, SELL a falling one). Outside the
   :attr:`open_start`–:attr:`open_end` window every epic stays flat.
2. **Cheap markets first** — a market whose spread is at most
   :attr:`max_spread_atr` quarter-hour ATRs (the measured variant) is preferred;
   a dearer one is only taken when there are not enough cheap ones.
3. **Variety first** — :meth:`filter_ranked` fills the basket with one market
   per asset class (index, forex, energy, agri, metal) before taking a second of
   any class, and never takes two markets sharing an exposure
   (:mod:`src.core.exposure`: a currency, an index region, a commodity
   underlying) — EUR/JPY and EUR/GBP bought together are one bet on the euro.
   On top, the morning one-minute returns must correlate at most
   :attr:`max_abs_correlation` (absolute) with every market kept, relaxed to
   :attr:`fallback_abs_correlation` only to fill a short basket. A hedge (two
   correlated markets traded opposite ways) is as unwelcome as a duplicate.
4. **Settled pool** — :meth:`filter_ranked` keeps nothing on the first
   in-window candle (:attr:`settle_minutes`): the 10:00 candle wave is still
   landing at that tick, so the ranking would only see the first few markets.
5. **One series a day** — ``require_flat_book`` opens the three in one pass and
   ``one_series_per_day`` keeps the selector shut for the rest of the day once
   anything has been opened, so a basket closed at 10:05 is not followed by a
   second one at 10:06.

The quarter-hour ATR comes from :func:`~src.core.indicators.estimated_timeframe_atr`:
the buffer only holds today's candles, so after a late start (or on a market that
opened at 09:00) there are too few 15-minute bars at 10:00 and the scaled
one-minute ATR stands in.

.. warning::

   **Not an edge.** Measured on the daily-basket replay (2026-10-23, W24–W42,
   54 usable days): no selection of this kind beats a coin flip on the same
   three markets (best z = +1.61 over 96 variants). Closing the book at +1 ATR15
   gave +0.14 ATR15 per day, not significant. The replay only kept days with
   three cheap unrelated markets; the fallbacks above are not measured.

Documented in ``docs/strategies/open_basket.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from src.core.exposure import exposure_of
from src.core.indicators import efficiency_ratio, estimated_timeframe_atr
from src.core.similarity import shape_correlation, shape_signature
from src.entry.base import EntryIntent, EntryStrategy
from src.feed.price_buffer import Candle, EpicBuffer
from src.stops.stop_far import FAR_STOP_BAR_MINUTES

logger = logging.getLogger(__name__)

PARIS = ZoneInfo("Europe/Paris")


@dataclass
class OpenBasket(EntryStrategy):
    """From 10:00 Paris, open the three best-ranked unrelated morning moves."""

    name = "open_basket"
    cross_epic_selection = True
    emits_shorts = True  # a falling morning is sold, a rising one bought

    # Rolling-selection constants (read by the scheduler). The portfolio model is
    # "one basket of three a day": count-bounded at three, opened in one pass, no
    # new basket while one is open nor once the day has had one.
    concurrent_positions = 3
    wallet_bounded = False
    open_cooldown_minutes = 0
    require_flat_book = True
    one_series_per_day = True
    wallet_reserve = 0.10
    # A ranking of three needs a pool several times that size to choose from.
    min_participation_ratio = 0.5
    min_participation_count = 10

    # --- Timing (Europe/Paris wall clock) ------------------------------------
    open_start: time = time(10, 0)  # first minute the basket may open
    # Last minute: a bot started late still opens the day's basket, up to the
    # basket close's 11:00 review — past it, the day is not worth starting.
    open_end: time = time(11, 0)
    # The basket is not chosen on the first in-window candle: at that tick the
    # 10:00 candle wave is still landing (2026-10-29: the 10:01:00 pass saw the
    # Nikkei's 10:00 candle and none of the 38 others, and opened a basket of
    # one). Waiting until a candidate holds the next minute gives every market a
    # full minute to deliver its first in-window candle.
    settle_minutes: int = 1
    morning_start: time = time(8, 0)  # the morning the move is measured on
    min_morning_candles: int = 60  # a market quoted < 1 h this morning is skipped

    # --- Scale and cost ------------------------------------------------------
    bar_minutes: int = FAR_STOP_BAR_MINUTES  # ATR15: the unit of the whole idea
    atr_period: int = 14
    max_spread_atr: float = 0.25  # preferred spread ceiling, in quarter-hour ATRs

    # --- Independence (see :meth:`filter_ranked`) ----------------------------
    max_abs_correlation: float = 0.3  # first pass: unrelated
    fallback_abs_correlation: float = 0.6  # second pass, only to fill the basket
    correlation_min_overlap: int = 30  # shared minutes for a correlation to count

    @property
    def warmup(self) -> int:
        return self.min_morning_candles + 1

    @classmethod
    def from_settings(cls, settings) -> OpenBasket:
        # All parameters are constants of this class (the dataclass field
        # defaults above), so the strategy builds from those and ignores
        # ``settings``.
        return cls()

    def _morning(self, candles: list[Candle]) -> list[Candle]:
        """The candles of the last candle's Paris day from :attr:`morning_start`."""
        if not candles:
            return []
        day = candles[-1].timestamp.astimezone(PARIS).date()
        start = datetime.combine(day, self.morning_start, tzinfo=PARIS)
        return [c for c in candles if c.timestamp >= start]

    def _atr15(self, candles: list[Candle]) -> float:
        return estimated_timeframe_atr(candles, self.bar_minutes, self.atr_period)

    def in_open_window(self, moment: datetime) -> bool:
        """True when ``moment`` falls in the Paris opening window."""
        local = moment.astimezone(PARIS).time()
        return self.open_start <= local < self.open_end

    def is_cheap(self, buf: EpicBuffer) -> bool:
        """True when the live spread is within :attr:`max_spread_atr` ATR15."""
        candles = list(buf.candles)
        if not candles:
            return False
        atr15 = self._atr15(candles)
        return atr15 > 0 and candles[-1].spread <= self.max_spread_atr * atr15

    def is_settled(self, ranked: list[tuple[EntryIntent, EpicBuffer]]) -> bool:
        """True once a candidate holds a candle :attr:`settle_minutes` past the open.

        A late start (the window already running) is settled from its first pass.
        """
        stamps = [buf.candles[-1].timestamp for _, buf in ranked if buf.candles]
        if not stamps:
            return False
        newest = max(stamps).astimezone(PARIS)
        opening = datetime.combine(newest.date(), self.open_start, tzinfo=PARIS)
        return newest >= opening + timedelta(minutes=self.settle_minutes)

    def evaluate(self, epic: str, buf: EpicBuffer) -> EntryIntent | None:
        candles = list(buf.candles)
        if len(candles) < self.warmup:
            return None
        last = candles[-1]
        if not self.in_open_window(last.timestamp):
            return None  # the basket is chosen from 10:00 and at no other time

        morning = self._morning(candles)
        if len(morning) < self.min_morning_candles:
            return None

        atr15 = self._atr15(candles)
        if atr15 <= 0 or last.bid_close <= 0 or last.spread <= 0:
            return None  # no volatility or no price: no stop can be sized

        bids = [c.bid_close for c in morning]
        move = (last.bid_close - morning[0].bid_open) / atr15
        if move == 0:
            return None
        er = efficiency_ratio(bids, len(bids) - 1)
        score = er * abs(move)
        direction = "BUY" if move > 0 else "SELL"
        logger.debug(
            "Basket %s: %s score=%.3f (ER=%.2f move=%+.2f ATR15, spread %.2f "
            "ATR15, %d morning candles)",
            epic,
            direction,
            score,
            er,
            move,
            last.spread / atr15,
            len(morning),
        )
        return EntryIntent(epic=epic, direction=direction, score=score)

    def filter_ranked(
        self, ranked: list[tuple[EntryIntent, EpicBuffer]]
    ) -> list[tuple[EntryIntent, EpicBuffer]]:
        """Order the ranking into the basket: varied first, then cheap, then score.

        ``ranked`` arrives best score first. Cheap markets (:meth:`is_cheap`) are
        moved ahead of dear ones, each group keeping its score order. The basket
        is then filled greedily in three passes over that order, each one only
        consulted while the basket is still short:

        1. **variety** — a new asset class, no exposure shared with a kept market
           (:func:`~src.core.exposure.exposure_of`: a currency, an index region, a
           commodity underlying) and morning returns correlated at most
           :attr:`max_abs_correlation`;
        2. same, but an asset class already in the basket is allowed;
        3. same, with the correlation allowed up to
           :attr:`fallback_abs_correlation`.

        A market sharing an exposure with a kept one is **never** taken: two
        pairs carrying the euro are one bet on the euro, however their morning
        curves happened to correlate. Everything accepted is returned in pass
        order, so the scheduler still has spares when a shared open gate refuses
        one.

        A candidate whose morning cannot be turned into a signature, or a pair
        that cannot be compared, counts as uncorrelated: a data gap must not
        shrink the basket.

        Nothing is kept while the newest candidate candle is still within
        :attr:`settle_minutes` of :attr:`open_start` (:meth:`is_settled`): the
        ranking would only see the markets whose first in-window candle happened
        to land before the selection tick.
        """
        if not self.is_settled(ranked):
            logger.info(
                "Basket: %d candidate(s) on the first in-window candle — waiting "
                "for the candle wave to land before choosing",
                len(ranked),
            )
            return []
        cheap = [item for item in ranked if self.is_cheap(item[1])]
        dear = [item for item in ranked if not self.is_cheap(item[1])]
        ordered = cheap + dear
        signatures = {
            id(item): shape_signature(item[0].epic, morning, len(morning))
            for item in ordered
            if (morning := self._morning(list(item[1].candles)))
        }
        exposures = {id(item): exposure_of(item[0].epic) for item in ordered}

        kept: list[tuple[EntryIntent, EpicBuffer]] = []

        def refusal(item, *, new_class: bool, ceiling: float) -> str | None:
            mine = exposures[id(item)]
            for other in kept:
                theirs = exposures[id(other)]
                if mine.shares_exposure(theirs):
                    shared = ", ".join(sorted(mine.tags & theirs.tags))
                    return f"same exposure as {other[0].epic} ({shared})"
                if new_class and mine.asset_class == theirs.asset_class:
                    return f"same asset class as {other[0].epic}"
                a, b = signatures.get(id(item)), signatures.get(id(other))
                if a is None or b is None:
                    continue
                corr = shape_correlation(a, b, min_overlap=self.correlation_min_overlap)
                if corr is not None and abs(corr) > ceiling:
                    return f"morning correlated with {other[0].epic} ({corr:.2f})"
            return None

        passes = (
            (True, self.max_abs_correlation),
            (False, self.max_abs_correlation),
            (False, self.fallback_abs_correlation),
        )
        why: dict[int, str] = {}
        for new_class, ceiling in passes:
            if len(kept) >= self.concurrent_positions:
                break
            for item in ordered:
                if any(item is k for k in kept):
                    continue
                reason = refusal(item, new_class=new_class, ceiling=ceiling)
                if reason is None:
                    kept.append(item)
                    why.pop(id(item), None)
                else:
                    why[id(item)] = reason

        chosen = kept[: self.concurrent_positions]
        for item in ordered[: 2 * self.concurrent_positions]:
            if id(item) in why:
                logger.info("Basket: skipping %s — %s", item[0].epic, why[id(item)])
        dear_ids = {id(item) for item in dear}
        dear_taken = sum(id(item) in dear_ids for item in chosen)
        logger.info(
            "Basket ranking: %d candidate(s), %d cheap (spread ≤ %.2f ATR15); "
            "basket %s%s",
            len(ranked),
            len(cheap),
            self.max_spread_atr,
            ", ".join(
                f"{item[0].epic} ({exposures[id(item)].asset_class})" for item in chosen
            )
            or "—",
            f" — {dear_taken} above the spread preference" if dear_taken else "",
        )
        return kept
