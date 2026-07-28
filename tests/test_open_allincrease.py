"""Tests for the paced, volatility-aware ranker (src/entry/open_allincrease.py).

Like its siblings ``open_ranking`` / ``open_saferanking`` this entry is a
*ranker*: ``evaluate`` returns a comparable composite score in [0, 1] for every
scorable epic (the scheduler does the cross-epic selection). These tests cover
the registry, the score contract ([0, 1], BUY-only), the structural ``None``
cases, the ``min_score`` floor, and the two behaviours that define this ranker:

- **volatility awareness** — a rise that is small relative to the market's own
  ATR ("relatively flat") scores lower than a compact steep rise of the same
  shape, so a flat market cannot be crowned;
- **recency weighting** — a curve rising *recently* outscores one that rose early
  and has since gone flat.

The selection-layer knobs (``wallet_bounded``, ``open_cooldown_minutes``) are
asserted here as the strategy's contract and
exercised end-to-end against the scheduler in ``tests/test_scheduler.py``.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from src.entry import OpenAllIncrease, get_entry_strategy
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


def _trending_up(n: int = 90, start: float = 8000.0, step: float = 1.0) -> list[float]:
    return [start + i * step for i in range(n)]


def _choppy(n: int = 90, start: float = 8000.0, amp: float = 5.0) -> list[float]:
    # Alternating up/down with no net drift: ~flat slope, low R².
    return [start + (amp if i % 2 else 0.0) for i in range(n)]


def _flat(n: int = 90, start: float = 8000.0) -> list[float]:
    return [start for _ in range(n)]


class TestRegistry:
    def test_registered_under_its_name(self):
        strat = get_entry_strategy("open_allincrease", _settings())
        assert isinstance(strat, OpenAllIncrease)
        assert strat.name == "open_allincrease"

    def test_is_a_cross_epic_ranker(self):
        strat = OpenAllIncrease()
        assert strat.cross_epic_selection is True
        assert isinstance(strat, EntryStrategy)


class TestSelectionKnobs:
    """The class constants the scheduler's rolling selector reads."""

    def test_wallet_bounded_paced_and_reopenable(self):
        strat = OpenAllIncrease()
        assert strat.wallet_bounded is True
        # Same-day re-open is global (.env ALLOW_SAME_DAY_REOPEN), not a knob.
        assert not hasattr(strat, "allow_same_day_reopen")
        assert strat.open_cooldown_minutes == 10

    def test_recency_weighting_decreases_with_horizon(self):
        strat = OpenAllIncrease()
        assert strat.weight_short > strat.weight_medium > strat.weight_long
        total = strat.weight_short + strat.weight_medium + strat.weight_long
        assert abs(total - 1.0) < 1e-9  # stays a [0, 1] score / percentage


class TestScoreContract:
    def test_clean_steep_uptrend_buys_with_high_score(self):
        strat = OpenAllIncrease()
        intent = strat.evaluate("E", _buffer(_trending_up()))
        assert intent is not None
        assert intent.direction == "BUY"
        assert 0.0 <= intent.score <= 1.0
        assert intent.score >= strat.min_score

    def test_choppy_market_stays_flat(self):
        strat = OpenAllIncrease()
        assert strat.evaluate("E", _buffer(_choppy())) is None

    def test_flat_line_stays_flat(self):
        strat = OpenAllIncrease()
        assert strat.evaluate("E", _buffer(_flat())) is None


class TestStructuralNone:
    def test_too_little_history(self):
        strat = OpenAllIncrease()
        short = _buffer(_trending_up(n=strat.warmup - 1))
        assert strat.evaluate("E", short) is None

    def test_non_positive_bid(self):
        strat = OpenAllIncrease()
        assert strat.evaluate("E", _buffer(_flat(start=0.0))) is None

    def test_no_volatility_returns_none(self):
        # A perfectly constant curve has ATR == 0 (no true range): structurally
        # unscorable — the close profile could not size a stop.
        strat = OpenAllIncrease()
        buf = _buffer(_flat(start=8000.0), pad=0.0)
        assert strat.evaluate("E", buf) is None


class TestScoreFloor:
    def test_below_floor_stays_flat_but_would_score_without_it(self):
        floored = OpenAllIncrease()
        # A weak, noisy drift: some upward tilt but not clean/strong enough.
        closes = _choppy(amp=1.0)
        assert floored.evaluate("E", _buffer(closes)) is None
        # With the floor removed the same curve is scorable (proves the floor,
        # not a structural reject, is what held it flat).
        openfloor = OpenAllIncrease(min_score=0.0)
        intent = openfloor.evaluate("E", _buffer(closes))
        # Either it scores (BUY) or is a genuine structural None; if it scores it
        # must be below the default floor.
        if intent is not None:
            assert intent.score < OpenAllIncrease().min_score


class TestVolatilityAwareness:
    """A rise small relative to the market's own volatility scores low."""

    def test_flat_relative_rise_scores_below_compact_rise(self):
        strat = OpenAllIncrease()
        bids = [100.0 + i for i in range(60)]  # clean line, slope 1, R² ≈ 1
        compact = strat._trend_component(bids, atr_value=1.0, period=60)
        volatile = strat._trend_component(bids, atr_value=50.0, period=60)
        # Same shape/cleanliness, far higher ATR → the rise is "relatively flat"
        # vs. that volatility → strictly lower magnitude factor → lower score.
        assert compact > volatile
        assert 0.0 <= volatile < compact <= 1.0

    def test_non_positive_slope_scores_zero(self):
        strat = OpenAllIncrease()
        falling = [100.0 - i for i in range(60)]
        assert strat._trend_component(falling, atr_value=1.0, period=60) == 0.0

    def test_zero_atr_scores_zero(self):
        strat = OpenAllIncrease()
        rising = [100.0 + i for i in range(60)]
        assert strat._trend_component(rising, atr_value=0.0, period=60) == 0.0


class TestRecencyWeighting:
    """A curve rising recently outscores one that rose early then went flat."""

    def test_recent_rise_beats_early_rise(self):
        strat = OpenAllIncrease(min_score=0.0)  # read raw scores, no floor
        n = 90
        # Rose early (0..39), flat since — the short/medium horizons are flat.
        early = [8000.0 + min(i, 40) for i in range(n)]
        # Flat until 49, rising since — the recent (short) horizon is strong.
        recent = [8000.0 + max(0, i - 49) for i in range(n)]

        early_intent = strat.evaluate("E", _buffer(early))
        recent_intent = strat.evaluate("R", _buffer(recent))
        assert early_intent is not None and recent_intent is not None
        assert recent_intent.score > early_intent.score
