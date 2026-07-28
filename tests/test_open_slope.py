"""Tests for the recent-slope ranker (src/entry/open_slope.py).

Like its siblings ``open_ranking`` / ``open_saferanking`` / ``open_allincrease``
/ ``open_rebound`` this entry is a *ranker*: ``evaluate`` returns a comparable
score for every scorable epic and the scheduler does the cross-epic selection.
These tests cover the registry, the score contract (a positive relative
progression, BUY-only, ranked by steepness), the long-only slope gate, the
structural ``None`` cases and the optional score floor.

The selection-layer knobs (``wallet_bounded``, ``open_cooldown_minutes``) are
asserted here as the strategy's contract; they are
exercised end-to-end against the scheduler in ``tests/test_scheduler.py``.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from src.entry import OpenSlope, get_entry_strategy
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


def _rise(n: int = 60, start: float = 8000.0, step: float = 1.0) -> list[float]:
    """A clean, monotonic climb of ``step`` points per candle."""
    return [start + i * step for i in range(n)]


def _fall(n: int = 60, start: float = 8100.0, step: float = 1.0) -> list[float]:
    return [start - i * step for i in range(n)]


class TestRegistry:
    def test_registered_under_its_name(self):
        strat = get_entry_strategy("open_slope", _settings())
        assert isinstance(strat, OpenSlope)
        assert strat.name == "open_slope"

    def test_is_a_cross_epic_ranker(self):
        strat = OpenSlope()
        assert strat.cross_epic_selection is True
        assert isinstance(strat, EntryStrategy)


class TestSelectionKnobs:
    """The class constants the scheduler's rolling selector reads (the spec)."""

    def test_wallet_bounded_paced_and_reopenable(self):
        strat = OpenSlope()
        assert strat.wallet_bounded is True  # open while the wallet allows
        assert strat.open_cooldown_minutes == 5  # a new open every 5 min at best
        # Same-day re-open is global (.env ALLOW_SAME_DAY_REOPEN), not a knob.
        assert not hasattr(strat, "allow_same_day_reopen")


class TestScoreContract:
    def test_rising_market_buys_with_positive_score(self):
        strat = OpenSlope()
        intent = strat.evaluate("E", _buffer(_rise()))
        assert intent is not None
        assert intent.direction == "BUY"
        assert intent.score > 0.0

    def test_steeper_rise_scores_higher(self):
        # The whole point of the ranker: a steeper recent slope ranks first.
        strat = OpenSlope()
        gentle = strat.evaluate("E", _buffer(_rise(step=1.0)))
        steep = strat.evaluate("E", _buffer(_rise(step=5.0)))
        assert gentle is not None and steep is not None
        assert steep.score > gentle.score

    def test_progression_is_relative_not_raw_slope(self):
        # Two epics with the SAME relative progression but different price scales
        # must score (almost) equally — proving the slope is normalised by price.
        strat = OpenSlope()
        cheap = strat.evaluate("E", _buffer(_rise(start=100.0, step=0.1)))
        pricey = strat.evaluate("E", _buffer(_rise(start=10000.0, step=10.0)))
        assert cheap is not None and pricey is not None
        # Both climb the same fraction per candle (0.1 % ish); scores are close.
        assert abs(cheap.score - pricey.score) < 1e-4


class TestLongOnlyGate:
    def test_falling_market_stays_flat(self):
        # A non-positive recent slope is not a rising market -> long-only, None.
        strat = OpenSlope()
        assert strat.evaluate("E", _buffer(_fall())) is None

    def test_flat_recent_slope_stays_flat(self):
        # Rose earlier but the last ~10 min are flat: recent slope 0 -> None.
        closes = _rise(n=50) + [8049.0] * 20
        strat = OpenSlope()
        assert strat.evaluate("E", _buffer(closes)) is None


class TestStructuralNone:
    def test_too_little_history(self):
        strat = OpenSlope()
        short = _buffer(_rise(n=strat.warmup - 1))
        assert strat.evaluate("E", short) is None

    def test_non_positive_bid(self):
        strat = OpenSlope()
        closes = _rise()
        closes[-1] = 0.0  # non-positive latest bid
        assert strat.evaluate("E", _buffer(closes)) is None

    def test_no_volatility_returns_none(self):
        # A perfectly constant curve has ATR == 0 (no true range): structurally
        # unscorable — the close profile could not size a stop.
        strat = OpenSlope()
        buf = _buffer([8000.0] * 60, pad=0.0)
        assert strat.evaluate("E", buf) is None


class TestScoreFloor:
    def test_below_floor_stays_flat(self):
        # A high floor rejects the same rise that scores without it, proving the
        # floor (not a structural reject) is what held it flat.
        floored = OpenSlope(min_score=0.99)
        assert floored.evaluate("E", _buffer(_rise())) is None
        openfloor = OpenSlope(min_score=0.0)
        assert openfloor.evaluate("E", _buffer(_rise())) is not None
