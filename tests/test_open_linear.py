"""Tests for the linear-day ranker (src/entry/open_linear.py).

Like its siblings ``open_ranking`` / ``open_saferanking`` / ``open_allincrease``
/ ``open_rebound`` / ``open_slope`` this entry is a *ranker*: ``evaluate`` returns
a comparable score in [0, 1] for every scorable epic and the scheduler does the
cross-epic selection. These tests cover the registry, the score contract (a
cleaner/straighter rising day scores higher), the shape gates (a straight line
beats a choppy or bending one; a flat straight line is held down by the strength
term), the long-only day-trend gate, the structural ``None`` cases and the
optional score floor.

The selection-layer knobs (``wallet_bounded``, ``open_cooldown_minutes``,
``allow_same_day_reopen``) are asserted here as the strategy's contract; they are
exercised end-to-end against the scheduler in ``tests/test_scheduler.py``.
"""

import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from src.entry import OpenLinear, get_entry_strategy
from src.entry.base import EntryStrategy
from src.feed.price_buffer import Candle, EpicBuffer


def _settings(**overrides) -> SimpleNamespace:
    # The ranker's parameters are class constants, so ``from_settings`` ignores
    # settings entirely; this stand-in only needs to exist for the registry call.
    return SimpleNamespace(**overrides)


def _buffer(closes: list[float], spread: float = 0.5, pad: float = 0.1) -> EpicBuffer:
    """Build a buffer from bid closes, with ±``pad`` intra-candle high/low."""
    buf = EpicBuffer(epic="TEST.EPIC", max_candles=len(closes) + 10)
    start = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)
    prev = closes[0]
    for i, close in enumerate(closes):
        high = max(prev, close) + pad
        low = min(prev, close) - pad
        buf.add(
            Candle(
                timestamp=start + timedelta(minutes=i),
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
    return buf


def _line(n: int = 60, start: float = 8000.0, step: float = 1.0) -> list[float]:
    """A clean, ruler-straight climb of ``step`` points per candle."""
    return [start + i * step for i in range(n)]


def _choppy(n: int = 60, start: float = 8000.0, step: float = 1.0) -> list[float]:
    """A rising day that wanders — same net rise, saw-tooth path (low ER)."""
    return [start + i * step + (6.0 if i % 2 else -6.0) for i in range(n)]


def _bending(n: int = 60, start: float = 8000.0) -> list[float]:
    """A rising day that curves (accelerating parabola) — fits a line poorly."""
    return [start + (i * 0.02) ** 2 for i in range(n)]


def _fall(n: int = 60, start: float = 8100.0, step: float = 1.0) -> list[float]:
    return [start - i * step for i in range(n)]


class TestRegistry:
    def test_registered_under_its_name(self):
        strat = get_entry_strategy("open_linear", _settings())
        assert isinstance(strat, OpenLinear)
        assert strat.name == "open_linear"

    def test_is_a_cross_epic_ranker(self):
        strat = OpenLinear()
        assert strat.cross_epic_selection is True
        assert isinstance(strat, EntryStrategy)


class TestSelectionKnobs:
    """The class constants the scheduler's rolling selector reads (the spec)."""

    def test_wallet_bounded_paced_and_once_per_day(self):
        strat = OpenLinear()
        assert strat.wallet_bounded is True  # open while the wallet allows
        assert strat.open_cooldown_minutes == 5  # a new open every 5 min at best
        assert strat.allow_same_day_reopen is False  # one opening per epic per day

    def test_weights_sum_to_one(self):
        # The composite is a weighted sum kept in [0, 1] / readable as a percentage.
        strat = OpenLinear()
        total = strat.weight_linearity + strat.weight_efficiency + strat.weight_strength
        assert math.isclose(total, 1.0)


class TestScoreContract:
    def test_clean_rising_line_buys_with_positive_score(self):
        strat = OpenLinear()
        intent = strat.evaluate("E", _buffer(_line()))
        assert intent is not None
        assert intent.direction == "BUY"
        assert 0.0 < intent.score <= 1.0

    def test_straight_line_beats_choppy_day(self):
        # Same net rise, but the saw-tooth path is far less efficient/linear.
        strat = OpenLinear()
        line = strat.evaluate("E", _buffer(_line(step=1.0)))
        chop = strat.evaluate("E", _buffer(_choppy(step=1.0)))
        assert line is not None and chop is not None
        assert line.score > chop.score

    def test_straight_line_beats_bending_day(self):
        # A curve that accelerates fits a straight line poorly (lower R²).
        strat = OpenLinear()
        line = strat.evaluate("E", _buffer(_line()))
        bend = strat.evaluate("E", _buffer(_bending()))
        assert line is not None and bend is not None
        assert line.score > bend.score

    def test_steeper_clean_line_scores_higher(self):
        # Both are perfectly straight, so linearity/efficiency saturate alike; the
        # strength term (relative progression) breaks the tie for the steeper day.
        strat = OpenLinear()
        gentle = strat.evaluate("E", _buffer(_line(step=1.0)))
        steep = strat.evaluate("E", _buffer(_line(step=4.0)))
        assert gentle is not None and steep is not None
        assert steep.score > gentle.score


class TestLongOnlyGate:
    def test_falling_day_stays_flat(self):
        # A non-positive whole-session slope is not a rising line -> long-only, None.
        strat = OpenLinear()
        assert strat.evaluate("E", _buffer(_fall())) is None


class TestStructuralNone:
    def test_too_little_history(self):
        strat = OpenLinear()
        short = _buffer(_line(n=strat.warmup - 1))
        assert strat.evaluate("E", short) is None

    def test_non_positive_bid(self):
        strat = OpenLinear()
        closes = _line()
        closes[-1] = 0.0  # non-positive latest bid
        assert strat.evaluate("E", _buffer(closes)) is None

    def test_no_volatility_returns_none(self):
        # A perfectly constant curve has ATR == 0 (no true range): structurally
        # unscorable — the close profile could not size a stop.
        strat = OpenLinear()
        buf = _buffer([8000.0] * 60, pad=0.0)
        assert strat.evaluate("E", buf) is None


class TestScoreFloor:
    def test_below_floor_stays_flat(self):
        # A high floor rejects the same clean line that scores without it, proving
        # the floor (not a structural reject) is what held it flat.
        floored = OpenLinear(min_score=0.999)
        assert floored.evaluate("E", _buffer(_line())) is None
        openfloor = OpenLinear(min_score=0.0)
        assert openfloor.evaluate("E", _buffer(_line())) is not None
