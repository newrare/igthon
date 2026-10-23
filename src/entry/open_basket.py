"""Cross-epic ranker — one basket of three unrelated markets, opened at 10:00.

The daily-basket idea: once a day, at 10:00 Paris, pick the three markets whose
morning looks the most promising, make sure they have nothing to do with each
other, open them together behind a far stop (``stop_far``) and let the
``basket`` close profile take the whole book off as soon as it is green enough
— or at 17:30 at the latest, with a review notification at 11:00 when the
morning did not pay.

Three things define it, and only the first is a per-epic decision:

1. **Score the morning** — :meth:`evaluate` reads the epic's candles since
   :attr:`morning_start` and scores the move by ``ER × |move| / ATR15``: the
   Kaufman efficiency of the morning path times the size of the move since the
   morning open, in quarter-hour ATRs. A large *and* clean move ranks first; the
   side follows the move (BUY a rising morning, SELL a falling one). Outside the
   :attr:`open_start`–:attr:`open_end` window every epic stays flat, so the
   ranking only ever happens at 10:00.
2. **Three unrelated markets** — :meth:`filter_ranked` keeps a candidate only
   when the absolute correlation of its morning one-minute returns with every
   market already kept is at most :attr:`max_abs_correlation`. The test is on
   the absolute value, not on the direction-signed redundancy ``open_five``
   uses: the point here is three *independent* bets, so a hedge (two correlated
   markets traded opposite ways) is refused as well as a duplicate.
3. **One series a day** — ``require_flat_book`` opens the three in one pass and
   ``one_series_per_day`` keeps the selector shut for the rest of the day once
   anything has been opened, so a basket closed at 10:05 is not followed by a
   second one at 10:06.

A cost gate sits in front of the score: a market whose spread is more than
:attr:`max_spread_atr` quarter-hour ATRs is refused — at that cost the basket
starts too deep under water to be closed green quickly.

.. warning::

   **Not an edge.** Measured on the daily-basket replay (2026-10-23, W24–W42,
   54 usable days): no selection of this kind beats a coin flip on the same
   three markets (best z = +1.61 over 96 variants). Closing the book at +1 ATR15
   gave +0.14 ATR15 per day, not significant. The constants below are the
   measured variant, frozen as-is for forward validation.

Documented in ``docs/strategies/open_basket.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from src.core.indicators import efficiency_ratio, timeframe_atr
from src.core.similarity import ShapeSignature, shape_correlation, shape_signature
from src.entry.base import EntryIntent, EntryStrategy
from src.feed.price_buffer import Candle, EpicBuffer
from src.stops.stop_far import FAR_STOP_BAR_MINUTES

logger = logging.getLogger(__name__)

PARIS = ZoneInfo("Europe/Paris")


@dataclass
class OpenBasket(EntryStrategy):
    """At 10:00 Paris, open the three cleanest unrelated morning moves."""

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
    # The replay had a median of 16 candidates once the spread gate applied: a
    # ranking of three needs a pool several times that size to choose from.
    min_participation_ratio = 0.5
    min_participation_count = 10

    # --- Timing (Europe/Paris wall clock) ------------------------------------
    open_start: time = time(10, 0)  # first minute the basket may open
    open_end: time = time(10, 15)  # leeway for a late start; flat afterwards
    morning_start: time = time(8, 0)  # the morning the move is measured on
    min_morning_candles: int = 60  # a market quoted < 1 h this morning is skipped

    # --- Scale and cost ------------------------------------------------------
    bar_minutes: int = FAR_STOP_BAR_MINUTES  # ATR15: the unit of the whole idea
    atr_period: int = 14
    max_spread_atr: float = 0.25  # spread ceiling, in quarter-hour ATRs

    # --- Independence filter (see :meth:`filter_ranked`) ---------------------
    max_abs_correlation: float = 0.3
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

    def in_open_window(self, moment: datetime) -> bool:
        """True when ``moment`` falls in the Paris opening window."""
        local = moment.astimezone(PARIS).time()
        return self.open_start <= local < self.open_end

    def evaluate(self, epic: str, buf: EpicBuffer) -> EntryIntent | None:
        candles = list(buf.candles)
        if len(candles) < self.warmup:
            return None
        last = candles[-1]
        if not self.in_open_window(last.timestamp):
            return None  # the basket is chosen at 10:00 and at no other time

        morning = self._morning(candles)
        if len(morning) < self.min_morning_candles:
            return None

        atr15 = timeframe_atr(candles, self.bar_minutes, self.atr_period)
        spread = last.spread
        if atr15 <= 0 or last.bid_close <= 0 or spread <= 0:
            return None
        if spread > self.max_spread_atr * atr15:
            logger.debug(
                "Basket %s rejected: spread %.5g > %.2f × ATR15 %.5g",
                epic,
                spread,
                self.max_spread_atr,
                atr15,
            )
            return None

        bids = [c.bid_close for c in morning]
        move = (last.bid_close - morning[0].bid_open) / atr15
        if move == 0:
            return None
        er = efficiency_ratio(bids, len(bids) - 1)
        score = er * abs(move)
        direction = "BUY" if move > 0 else "SELL"
        logger.debug(
            "Basket %s: %s score=%.3f (ER=%.2f move=%+.2f ATR15, %d morning candles)",
            epic,
            direction,
            score,
            er,
            move,
            len(morning),
        )
        return EntryIntent(epic=epic, direction=direction, score=score)

    def filter_ranked(
        self, ranked: list[tuple[EntryIntent, EpicBuffer]]
    ) -> list[tuple[EntryIntent, EpicBuffer]]:
        """Keep, best first, the candidates unrelated to every one kept before.

        Greedy over the whole ranking, so a candidate related to a better one
        lets the next unrelated market move up into the basket. A candidate whose
        morning cannot be turned into a signature, or a pair that cannot be
        compared, is kept: a data gap must not shrink the basket.
        """
        kept: list[tuple[EntryIntent, EpicBuffer]] = []
        signatures: list[ShapeSignature] = []
        for intent, buf in ranked:
            morning = self._morning(list(buf.candles))
            signature = shape_signature(intent.epic, morning, len(morning))
            related = None
            if signature is not None:
                for other in signatures:
                    corr = shape_correlation(
                        signature, other, min_overlap=self.correlation_min_overlap
                    )
                    if corr is not None and abs(corr) > self.max_abs_correlation:
                        related = (other.epic, corr)
                        break
            if related is not None:
                logger.info(
                    "Basket: dropping %s — morning correlated with %s (%.2f, "
                    "|corr| > %.2f)",
                    intent.epic,
                    related[0],
                    related[1],
                    self.max_abs_correlation,
                )
                continue
            kept.append((intent, buf))
            if signature is not None:
                signatures.append(signature)
        return kept
