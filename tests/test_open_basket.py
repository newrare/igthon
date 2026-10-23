"""Tests for the 10:00 basket ranker (src/entry/open_basket.py).

``evaluate`` scores one epic's morning (window, cost gate, score, side);
``filter_ranked`` keeps three unrelated markets. The selection constants
(``require_flat_book``, ``one_series_per_day``) are exercised against the
scheduler in ``tests/test_scheduler.py`` (``TestOneSeriesPerDay``).
"""

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.entry import OpenBasket, get_entry_strategy
from src.entry.base import EntryIntent
from src.feed.price_buffer import Candle, EpicBuffer

PARIS = ZoneInfo("Europe/Paris")
_MORNING = datetime(2026, 10, 22, 8, 0, tzinfo=PARIS)


def _buffer(
    closes: list[float],
    *,
    epic: str = "TEST.EPIC",
    spread: float = 0.05,
    start: datetime | None = None,
) -> EpicBuffer:
    """One candle per minute from ``start`` (default 08:00 Paris)."""
    buf = EpicBuffer(epic=epic, max_candles=len(closes) + 10)
    stamp = start or _MORNING
    prev = closes[0]
    for close in closes:
        high = max(prev, close) + 0.1
        low = min(prev, close) - 0.1
        buf.add(
            Candle(
                timestamp=stamp,
                bid_open=prev,
                bid_close=close,
                bid_high=high,
                bid_low=low,
                offer_open=prev + spread,
                offer_close=close + spread,
                offer_high=high + spread,
                offer_low=low + spread,
            )
        )
        prev = close
        stamp += timedelta(minutes=1)
    return buf


def _wavy(
    count: int, slope: float, *, period: int = 7, phase: float = 0.0
) -> list[float]:
    """A trend with an oscillation, so ATR15 > 0 and ER < 1."""
    return [
        100.0 + slope * i + 0.4 * math.sin(2 * math.pi * i / period + phase)
        for i in range(count)
    ]


# 08:00 → 10:01 Paris: 121 candles, the last one inside the opening window.
_TO_TEN = 121


class TestContract:
    def test_registered(self):
        assert isinstance(get_entry_strategy("open_basket", object()), OpenBasket)

    def test_selection_constants(self):
        strategy = OpenBasket()
        assert strategy.cross_epic_selection
        assert strategy.emits_shorts
        assert strategy.concurrent_positions == 3
        assert strategy.require_flat_book
        assert strategy.one_series_per_day


class TestEvaluate:
    def test_rising_morning_is_bought(self):
        intent = OpenBasket().evaluate("E", _buffer(_wavy(_TO_TEN, 0.05)))

        assert intent is not None
        assert intent.direction == "BUY"
        assert intent.score > 0

    def test_falling_morning_is_sold(self):
        intent = OpenBasket().evaluate("E", _buffer(_wavy(_TO_TEN, -0.05)))

        assert intent is not None
        assert intent.direction == "SELL"

    def test_cleaner_move_scores_higher(self):
        clean = OpenBasket().evaluate("E", _buffer(_wavy(_TO_TEN, 0.05)))
        choppy = OpenBasket().evaluate("E", _buffer(_wavy(_TO_TEN, 0.01)))

        assert clean.score > choppy.score

    def test_flat_outside_the_opening_window(self):
        strategy = OpenBasket()
        # 08:00 → 09:59: one minute before the window.
        assert strategy.evaluate("E", _buffer(_wavy(119, 0.05))) is None
        # 08:00 → 10:20: past the 10:15 leeway.
        assert strategy.evaluate("E", _buffer(_wavy(141, 0.05))) is None

    def test_too_short_a_morning_is_skipped(self):
        # Opened at 09:10: 51 candles by 10:00.
        start = _MORNING + timedelta(minutes=70)
        buf = _buffer(_wavy(51, 0.05), start=start)

        assert OpenBasket().evaluate("E", buf) is None

    def test_expensive_market_is_refused(self):
        buf = _buffer(_wavy(_TO_TEN, 0.05), spread=5.0)

        assert OpenBasket().evaluate("E", buf) is None


class TestFilterRanked:
    @staticmethod
    def _ranked(*buffers: tuple[str, EpicBuffer]) -> list:
        return [
            (EntryIntent(epic=epic, direction="BUY", score=10.0 - i), buf)
            for i, (epic, buf) in enumerate(buffers)
        ]

    def test_drops_a_market_correlated_with_a_better_one(self):
        a = _buffer(_wavy(_TO_TEN, 0.05), epic="A")
        twin = _buffer(_wavy(_TO_TEN, 0.05), epic="TWIN")
        other = _buffer(_wavy(_TO_TEN, 0.05, period=11, phase=1.3), epic="OTHER")

        kept = OpenBasket().filter_ranked(
            self._ranked(("A", a), ("TWIN", twin), ("OTHER", other))
        )

        assert [intent.epic for intent, _ in kept] == ["A", "OTHER"]

    def test_anti_correlated_market_is_refused_too(self):
        a = _buffer(_wavy(_TO_TEN, 0.05), epic="A")
        mirror = _buffer([200.0 - c for c in _wavy(_TO_TEN, 0.05)], epic="MIRROR")

        kept = OpenBasket().filter_ranked(self._ranked(("A", a), ("MIRROR", mirror)))

        assert [intent.epic for intent, _ in kept] == ["A"]
