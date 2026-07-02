"""Tests for the Donchian + projection-consensus entry strategy
(src/entry/open_projection.py).

The strategy reuses the open_donchian gates (spread, efficiency regime, breakout)
and adds a hard projection-consensus gate. These tests exercise the registry,
the new gate (pass and reject), and the exit-agnostic intent contract.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from src.entry import EntryIntent, OpenProjection, get_entry_strategy
from src.entry.base import EntryStrategy
from src.feed.price_buffer import Candle, EpicBuffer


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "strategy_donchian_channel": 20,
        "strategy_atr_period": 14,
        "strategy_efficiency_period": 30,
        "strategy_min_efficiency": 0.45,
        "strategy_max_spread_ratio": 0.0015,
        "strategy_projection_horizon": 30,
        "strategy_projection_degree": 2,
        "strategy_projection_ema_span": 10,
        "strategy_projection_min_score": 0.50,
        "strategy_projection_weight_linear": 0.40,
        "strategy_projection_weight_polynomial": 0.30,
        "strategy_projection_weight_ema": 0.30,
        "strategy_projection_weight_exp": 0.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _buffer(closes: list[float], spread: float = 0.5) -> EpicBuffer:
    buf = EpicBuffer(epic="TEST.EPIC", max_candles=len(closes) + 10)
    start = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)
    prev = closes[0]
    for i, close in enumerate(closes):
        high = max(prev, close) + 0.1
        low = min(prev, close) - 0.1
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


def _trending_up(n: int = 80, start: float = 8000.0, step: float = 1.0) -> list[float]:
    return [start + i * step for i in range(n)]


class TestRegistry:
    def test_known_name_resolves(self):
        strat = get_entry_strategy("open_projection", _settings())
        assert isinstance(strat, OpenProjection)

    def test_parameters_are_class_constants(self):
        # from_settings ignores settings — parameters are class constants. The
        # overrides below are NOT picked up; the constructor still tunes them.
        strat = get_entry_strategy("open_projection", _settings())
        assert strat.projection_horizon == 30  # class constant
        assert strat.min_projection_score == 0.50
        assert strat.projection_weights["linear"] == 0.40
        custom = OpenProjection(
            projection_horizon=45, min_projection_score=0.7
        )
        assert custom.projection_horizon == 45
        assert custom.min_projection_score == 0.7


class TestEvaluate:
    def test_breakout_with_confirming_projection_emits_buy(self):
        intent = OpenProjection().evaluate(
            "TEST.EPIC", _buffer(_trending_up(80))
        )
        assert isinstance(intent, EntryIntent)
        assert intent.direction == "BUY"
        # The opening score is the projection consensus, not the ER.
        assert intent.score >= 0.5

    def test_breakout_rejected_when_projection_score_too_high_to_reach(self):
        # Same clean breakout, but demand a consensus the curve cannot supply
        # within the active models → the projection gate blocks the entry.
        strat = OpenProjection(min_projection_score=1.01)
        assert strat.evaluate("TEST.EPIC", _buffer(_trending_up(80))) is None

    def test_insufficient_warmup_returns_none(self):
        # Horizon drives warmup; 30 candles < warmup → no evaluation.
        assert (
            OpenProjection().evaluate("TEST.EPIC", _buffer(_trending_up(30)))
            is None
        )

    def test_is_entry_strategy_instance(self):
        assert isinstance(OpenProjection(), EntryStrategy)
