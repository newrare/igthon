"""Tests for the linear-day ranker (src/entry/open_linear.py).

Like its siblings ``open_ranking`` / ``open_saferanking`` / ``open_allincrease``
/ ``open_rebound`` / ``open_slope`` this entry is a *ranker*: ``evaluate`` returns
a comparable score in [0, 1] for every scorable epic and the scheduler does the
cross-epic selection. It is **two-sided**: a straight rising day is bought, a
straight falling day is sold. These tests cover the registry, the direction
contract (both sides, symmetric scoring), the score contract (a
cleaner/straighter day scores higher), the shape gates (a straight line beats a
choppy or bending one; a flat straight line is held down by the strength term),
the structural ``None`` cases and the optional score floor.

The selection-layer knobs (``wallet_bounded``, ``open_cooldown_minutes``) and the
``emits_shorts`` contract are asserted here as the strategy's contract; they are
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
    """A clean, ruler-straight fall of ``step`` points per candle."""
    return [start - i * step for i in range(n)]


def _mirror(closes: list[float]) -> list[float]:
    """Reflect a series around its first value — same shape, opposite direction."""
    return [2 * closes[0] - close for close in closes]


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

    def test_emits_shorts(self):
        # Two-sided: the scheduler must keep SELL intents and lift the long-only
        # pre-open gate for this ranker.
        assert OpenLinear().emits_shorts is True

    def test_wallet_bounded_paced_and_once_per_day(self):
        strat = OpenLinear()
        assert strat.wallet_bounded is True  # open while the wallet allows
        assert strat.open_cooldown_minutes == 5  # a new open every 5 min at best
        # One opening per epic per day is now the global .env policy
        # (ALLOW_SAME_DAY_REOPEN=false), not a strategy attribute.
        assert not hasattr(strat, "allow_same_day_reopen")

    def test_weights_sum_to_one(self):
        # The composite is a weighted sum kept in [0, 1] / readable as a percentage.
        strat = OpenLinear()
        total = strat.weight_linearity + strat.weight_efficiency + strat.weight_strength
        assert math.isclose(total, 1.0)


class TestDirection:
    """Two-sided: the sign of the day's slope picks the side, nothing else."""

    def test_clean_rising_line_buys_with_positive_score(self):
        strat = OpenLinear()
        intent = strat.evaluate("E", _buffer(_line()))
        assert intent is not None
        assert intent.direction == "BUY"
        assert 0.0 < intent.score <= 1.0

    def test_clean_falling_line_sells_with_positive_score(self):
        # The same ruler-straight shape traced downwards is the same setup, taken
        # from the short side (previously rejected by the long-only gate).
        strat = OpenLinear()
        intent = strat.evaluate("E", _buffer(_fall()))
        assert intent is not None
        assert intent.direction == "SELL"
        assert 0.0 < intent.score <= 1.0

    def test_mirrored_days_score_alike(self):
        # Every term is direction-agnostic (R², |net|/Σ|step|, |net move|), so a
        # mirrored day scores the same up to the price-relative strength
        # denominator (the two paths end at different bids).
        strat = OpenLinear()
        up = strat.evaluate("E", _buffer(_line()))
        down = strat.evaluate("E", _buffer(_mirror(_line())))
        assert up is not None and down is not None
        assert up.direction == "BUY"
        assert down.direction == "SELL"
        assert math.isclose(up.score, down.score, abs_tol=0.01)

    def test_flat_day_stays_flat(self):
        # A zero slope gives no side to take — the sole directional reject. The
        # curve still moves (non-zero ATR) and the floor is disabled, so ``None``
        # can only come from the direction gate.
        strat = OpenLinear(min_score=0.0)
        n = 60
        symmetric_v = [8000.0 + abs(i - (n - 1) / 2) for i in range(n)]  # slope == 0
        assert strat.evaluate("E", _buffer(symmetric_v)) is None

    def test_nearly_flat_line_is_held_down_by_strength_not_gated(self):
        # A straight but almost-flat climb is not gated on direction: it scores,
        # and scores below a steeper line of the same shape.
        strat = OpenLinear(min_score=0.0)
        flat = strat.evaluate("E", _buffer(_line(step=0.01)))
        steep = strat.evaluate("E", _buffer(_line(step=2.0)))
        assert flat is not None and steep is not None
        assert flat.score < steep.score


class TestScoreContract:
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

    def test_straight_fall_beats_choppy_fall(self):
        # The shape terms discriminate on the short side exactly as on the long one.
        strat = OpenLinear()
        line = strat.evaluate("E", _buffer(_mirror(_line(step=1.0))))
        chop = strat.evaluate("E", _buffer(_mirror(_choppy(step=1.0))))
        assert line is not None and chop is not None
        assert line.direction == chop.direction == "SELL"
        assert line.score > chop.score

    def test_steeper_clean_fall_scores_higher(self):
        strat = OpenLinear()
        gentle = strat.evaluate("E", _buffer(_fall(step=1.0)))
        steep = strat.evaluate("E", _buffer(_fall(step=4.0)))
        assert gentle is not None and steep is not None
        assert steep.score > gentle.score


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

    def test_floor_applies_to_shorts_too(self):
        floored = OpenLinear(min_score=0.999)
        assert floored.evaluate("E", _buffer(_fall())) is None
        openfloor = OpenLinear(min_score=0.0)
        assert openfloor.evaluate("E", _buffer(_fall())) is not None
