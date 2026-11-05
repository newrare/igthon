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


# 08:00 → 10:01 Paris: 122 candles, the last one inside the opening window and
# one minute past its first candle (the basket's settle wait).
_TO_TEN = 122


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
        # 08:00 → 11:01: past the 11:00 end of the window.
        assert strategy.evaluate("E", _buffer(_wavy(182, 0.05))) is None

    def test_a_late_start_still_opens_before_eleven(self):
        # 08:00 → 10:40: the bot came up late, the basket is still due.
        assert OpenBasket().evaluate("E", _buffer(_wavy(161, 0.05))) is not None

    def test_a_short_session_is_still_scored(self):
        # Bot started at 08:27 (2026-10-26): 95 candles at 10:01, six
        # quarter-hour bars — fewer than the 14-bar ATR would want.
        start = _MORNING + timedelta(minutes=27)

        intent = OpenBasket().evaluate("E", _buffer(_wavy(95, 0.05), start=start))

        assert intent is not None

    def test_too_short_a_morning_is_skipped(self):
        # Opened at 09:10: 51 candles by 10:00.
        start = _MORNING + timedelta(minutes=70)
        buf = _buffer(_wavy(51, 0.05), start=start)

        assert OpenBasket().evaluate("E", buf) is None

    def test_expensive_market_is_still_scored(self):
        buf = _buffer(_wavy(_TO_TEN, 0.05), spread=5.0)

        assert OpenBasket().evaluate("E", buf) is not None
        assert not OpenBasket().is_cheap(buf)


class TestFilterRanked:
    @staticmethod
    def _ranked(*buffers: tuple[str, EpicBuffer]) -> list:
        return [
            (EntryIntent(epic=epic, direction="BUY", score=10.0 - i), buf)
            for i, (epic, buf) in enumerate(buffers)
        ]

    def test_waits_on_the_first_in_window_candle(self):
        # 2026-10-29: at 10:01:00 only the Nikkei held its 10:00 candle, the
        # other markets' 10:00 candles landed just after the selection tick.
        first = _buffer(_wavy(121, 0.05), epic="FIRST")

        assert OpenBasket().filter_ranked(self._ranked(("FIRST", first))) == []

    def test_chooses_once_a_candidate_holds_the_next_minute(self):
        late = _buffer(_wavy(121, 0.05), epic="LATE")  # last candle 10:00
        ahead = _buffer(_wavy(_TO_TEN, 0.05, period=11, phase=1.3), epic="AHEAD")

        kept = OpenBasket().filter_ranked(
            self._ranked(("LATE", late), ("AHEAD", ahead))
        )

        assert [intent.epic for intent, _ in kept] == ["LATE", "AHEAD"]

    def test_a_late_start_chooses_on_its_first_pass(self):
        buf = _buffer(_wavy(161, 0.05), epic="E")  # 08:00 → 10:40

        assert len(OpenBasket().filter_ranked(self._ranked(("E", buf)))) == 1

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

    def test_cheap_markets_rank_ahead_of_dear_ones(self):
        dear = _buffer(_wavy(_TO_TEN, 0.05), epic="DEAR", spread=5.0)
        cheap = _buffer(_wavy(_TO_TEN, 0.05, period=11, phase=1.3), epic="CHEAP")

        kept = OpenBasket().filter_ranked(
            self._ranked(("DEAR", dear), ("CHEAP", cheap))
        )

        assert [intent.epic for intent, _ in kept] == ["CHEAP", "DEAR"]

    def test_a_short_basket_is_filled_with_a_moderately_related_market(self):
        a_closes = _wavy(_TO_TEN, 0.05)
        b_closes = _wavy(_TO_TEN, 0.05, period=11, phase=1.3)
        # 30 % of A blended into B: |corr| with A ≈ 0.57, between the 0.3
        # preference and the 0.6 ceiling.
        blend = [0.7 * y + 0.3 * x for x, y in zip(a_closes, b_closes)]
        a = _buffer(a_closes, epic="A")
        near = _buffer(blend, epic="NEAR")

        kept = OpenBasket().filter_ranked(self._ranked(("A", a), ("NEAR", near)))

        assert [intent.epic for intent, _ in kept] == ["A", "NEAR"]

    def test_a_full_basket_does_not_take_the_related_market(self):
        a_closes = _wavy(_TO_TEN, 0.05)
        blend = [
            0.7 * y + 0.3 * x
            for x, y in zip(a_closes, _wavy(_TO_TEN, 0.05, period=11, phase=1.3))
        ]
        ranked = self._ranked(
            ("A", _buffer(a_closes, epic="A")),
            ("NEAR", _buffer(blend, epic="NEAR")),
            ("B", _buffer(_wavy(_TO_TEN, -0.03, period=5, phase=0.4), epic="B")),
            ("C", _buffer(_wavy(_TO_TEN, 0.02, period=13, phase=2.1), epic="C")),
        )
        strategy = OpenBasket()
        unrelated = strategy.filter_ranked([ranked[0], ranked[2], ranked[3]])
        assert len(unrelated) == 3  # precondition: A, B, C are unrelated

        kept = strategy.filter_ranked(ranked)

        assert [intent.epic for intent, _ in kept] == ["A", "B", "C"]

    def test_two_pairs_sharing_a_currency_are_never_both_taken(self):
        # Unrelated curves: only the shared euro links them.
        ranked = [
            (
                EntryIntent(epic="CS.D.EURJPY.CFD.IP", direction="BUY", score=3.0),
                _buffer(_wavy(_TO_TEN, 0.05), epic="CS.D.EURJPY.CFD.IP"),
            ),
            (
                EntryIntent(epic="CS.D.EURGBP.CFD.IP", direction="BUY", score=2.0),
                _buffer(
                    _wavy(_TO_TEN, 0.05, period=11, phase=1.3),
                    epic="CS.D.EURGBP.CFD.IP",
                ),
            ),
        ]

        kept = OpenBasket().filter_ranked(ranked)

        assert [intent.epic for intent, _ in kept] == ["CS.D.EURJPY.CFD.IP"]

    def test_a_new_asset_class_outranks_a_better_scored_same_class(self):
        epics = ("CS.D.EURJPY.CFD.IP", "CS.D.GBPUSD.CFD.IP", "CO.D.BO.FWM4.IP")
        shapes = (
            _wavy(_TO_TEN, 0.05),
            _wavy(_TO_TEN, 0.05, period=11, phase=1.3),
            _wavy(_TO_TEN, 0.02, period=13, phase=2.1),
        )
        ranked = [
            (
                EntryIntent(epic=epic, direction="BUY", score=3.0 - i),
                _buffer(shape, epic=epic),
            )
            for i, (epic, shape) in enumerate(zip(epics, shapes))
        ]

        kept = OpenBasket().filter_ranked(ranked)

        # Soybean oil (agri) comes before the second forex pair.
        assert [intent.epic for intent, _ in kept] == [
            "CS.D.EURJPY.CFD.IP",
            "CO.D.BO.FWM4.IP",
            "CS.D.GBPUSD.CFD.IP",
        ]
