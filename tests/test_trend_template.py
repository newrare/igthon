"""Tests for the hourly trend-template strategy (src/strategies/trend_template.py).

Covers the per-epic composite scoring (R²-dominant shape, spread tightness and
reachability as soft components rather than pass/fail gates), the structural
``None`` cases (insufficient data), the spread-multiple take-profit and ATR
stop, the settings mapping, the martingale ``compute_quantity_multiplier``
helper, and that ``open_position`` scales the deal size by the multiplier.
"""

from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.indicators import (
    RegressionResult,
    TradingLevels,
    TradingSignal,
    atr,
    linear_regression,
)
from src.execution.trading import (
    TradeConfig,
    TradingService,
    compute_quantity_multiplier,
)
from src.feed.price_buffer import Candle, EpicBuffer
from src.strategies import TrendTemplate, get_strategy
from src.strategies.trend_template import projected_reachable


def _settings(**overrides) -> SimpleNamespace:
    """Settings stand-in with every attribute the strategy reads."""
    base = {
        "strategy_trend_template_regression_period": 30,
        "strategy_trend_template_min_r2": 0.80,
        "strategy_trend_template_win_ratio": 2.0,
        "strategy_trend_template_projection_horizon": 60,
        "strategy_trend_template_stop_lookback": 60,
        "strategy_trend_template_stop_buffer_atr_k": 0.5,
        "strategy_atr_period": 14,
        "strategy_max_spread_ratio": 0.0015,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _buffer(closes: list[float], spread: float = 0.5) -> EpicBuffer:
    """Build a buffer of synthetic candles from a list of bid closes."""
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


def _line(n: int = 70, start: float = 8000.0, step: float = 2.0) -> list[float]:
    """A clean straight rising (or falling) line — R² ≈ 1.0, slope = step."""
    return [start + i * step for i in range(n)]


class TestSettings:
    def test_from_settings_maps_parameters(self):
        strat = TrendTemplate.from_settings(
            _settings(
                strategy_trend_template_win_ratio=3.0,
                strategy_trend_template_min_r2=0.9,
            )
        )
        assert strat.win_ratio == 3.0
        assert strat.min_r2 == 0.9
        assert strat.atr_period == 14

    def test_registry_resolves_by_name(self):
        strat = get_strategy("trend_template", _settings())
        assert isinstance(strat, TrendTemplate)

    def test_warmup_covers_widest_window(self):
        strat = TrendTemplate(regression_period=30, stop_lookback=60, atr_period=14)
        assert strat.warmup == 61

    def test_hourly_selection_flag_is_set(self):
        # Drives the scheduler to use the cross-epic job instead of per-epic opens.
        assert TrendTemplate().hourly_selection is True


class TestEntry:
    def test_clean_uptrend_emits_buy_with_high_composite_score(self):
        strat = TrendTemplate()
        buf = _buffer(_line())
        signal = strat.evaluate("TEST.EPIC", buf)
        assert signal is not None
        assert signal.direction == "BUY"
        # The score is a composite in [0, 1]; a clean, tight, reachable up-trend
        # scores near the top (R²-dominant weight on a near-perfect fit).
        assert 0.0 <= signal.score <= 1.0
        assert signal.score > 0.95

    def test_take_profit_is_net_win_ratio_spreads(self):
        spread = 0.5
        strat = TrendTemplate(win_ratio=2.0)
        signal = strat.evaluate("TEST.EPIC", _buffer(_line(), spread=spread))
        assert signal is not None
        # level_win = bid + spread (break-even) + win_ratio × spread (net gain).
        expected = signal.levels.bid + (1 + 2.0) * spread
        assert signal.levels.level_win == pytest.approx(expected)
        # Protective stop sits below the bid; loose/follower pinned to it.
        assert signal.levels.level_security < signal.levels.bid
        assert signal.levels.level_loose == signal.levels.level_security
        assert signal.levels.level_follower == signal.levels.level_security

    def test_insufficient_data_returns_none(self):
        # The only structural rejection: too little data to fit the curve.
        strat = TrendTemplate()
        assert strat.evaluate("TEST.EPIC", _buffer(_line(10))) is None

    def test_flat_market_is_scored_not_rejected(self):
        # Zero slope is no longer a rejection: the market is still scored (low)
        # so the scheduler can open it if it is the best available that hour.
        strat = TrendTemplate()
        signal = strat.evaluate("TEST.EPIC", _buffer([8000.0] * 70))
        assert signal is not None
        assert signal.direction == "BUY"
        # No up-trend → the dominant shape component contributes nothing.
        assert signal.score < 0.5

    def test_downtrend_scores_below_uptrend(self):
        # Negative slope earns a zero shape score → ranks well below an up-trend,
        # but is scored rather than rejected.
        strat = TrendTemplate()
        down = strat.evaluate("TEST.EPIC", _buffer(_line(step=-2.0)))
        up = strat.evaluate("TEST.EPIC", _buffer(_line(step=2.0)))
        assert down is not None and up is not None
        assert down.score < up.score

    def test_below_min_r2_loses_the_shape_component(self):
        strat = TrendTemplate(min_r2=0.80)
        # Mild up-drift drowned by a large alternating swing → low R².
        closes = [8000.0 + i * 0.5 + (60.0 if i % 2 else -60.0) for i in range(70)]
        reg = linear_regression(closes[-strat.regression_period :])
        assert reg.r_squared < strat.min_r2  # precondition for this test
        noisy = strat.evaluate("TEST.EPIC", _buffer(closes))
        clean = strat.evaluate("TEST.EPIC", _buffer(_line()))
        assert noisy is not None
        # Below min_r2 the shape component is 0, so it cannot beat a clean trend.
        assert noisy.score < clean.score

    def test_wide_spread_zeroes_the_spread_component(self):
        strat = TrendTemplate(max_spread_ratio=0.0001)
        tight = strat.evaluate("TEST.EPIC", _buffer(_line(), spread=0.5))
        wide = strat.evaluate("TEST.EPIC", _buffer(_line(), spread=5.0))
        assert tight is not None and wide is not None
        # Identical shape, but a spread above the ceiling loses that component.
        assert wide.score < tight.score


class TestStop:
    def test_stop_sits_at_last_hour_support_with_cushion(self):
        # The stop is anchored to the lowest bid low of the last STOP_LOOKBACK
        # candles minus the ATR cushion — never pulled up close to the entry.
        strat = TrendTemplate(stop_lookback=60, stop_buffer_atr_k=0.5)
        buf = _buffer(_line())
        signal = strat.evaluate("TEST.EPIC", buf)
        assert signal is not None
        candles = list(buf.candles)
        support = min(c.bid_low for c in candles[-strat.stop_lookback :])
        atr_value = atr(candles, strat.atr_period)
        assert signal.levels.level_security == pytest.approx(support - 0.5 * atr_value)

    def test_stop_not_capped_when_support_is_far(self):
        # A steep, sustained climb leaves the hour's support far below the bid.
        # The old ATR cap (3×ATR) would have clamped the stop much closer; now
        # the stop honours the real (far) support, so the distance exceeds it.
        strat = TrendTemplate(stop_lookback=60, stop_buffer_atr_k=0.5)
        buf = _buffer(_line(step=5.0))
        signal = strat.evaluate("TEST.EPIC", buf)
        assert signal is not None
        atr_value = atr(list(buf.candles), strat.atr_period)
        distance = signal.levels.bid - signal.levels.level_security
        assert distance > 3.0 * atr_value


class TestProjection:
    def test_reachable_when_slope_covers_distance(self):
        assert projected_reachable(slope=2.0, distance=1.5, horizon=60) is True

    def test_unreachable_when_slope_too_shallow(self):
        assert projected_reachable(slope=0.01, distance=1.5, horizon=60) is False

    def test_non_positive_slope_never_reachable(self):
        assert projected_reachable(slope=0.0, distance=0.0, horizon=60) is False
        assert projected_reachable(slope=-1.0, distance=1.5, horizon=60) is False


def _closed(win: int, minute: int, pid: int) -> SimpleNamespace:
    """Lightweight stand-in for a closed Position (only read fields needed)."""
    return SimpleNamespace(
        win=win, time_close=time(9, minute), time_open=time(9, minute), id=pid
    )


class TestQuantityMultiplier:
    def test_no_trades_starts_at_one(self):
        assert (
            compute_quantity_multiplier([], base_multiplier=3, max_multiplier=27) == 1
        )

    def test_last_trade_won_resets_to_one(self):
        closed = [_closed(0, 1, 1), _closed(1, 2, 2)]  # loss then win (most recent)
        assert (
            compute_quantity_multiplier(closed, base_multiplier=3, max_multiplier=27)
            == 1
        )

    def test_single_trailing_loss_escalates(self):
        closed = [_closed(1, 1, 1), _closed(0, 2, 2)]  # win then loss (most recent)
        assert (
            compute_quantity_multiplier(closed, base_multiplier=3, max_multiplier=27)
            == 3
        )

    def test_two_consecutive_losses_compound(self):
        closed = [_closed(1, 1, 1), _closed(0, 2, 2), _closed(0, 3, 3)]
        assert (
            compute_quantity_multiplier(closed, base_multiplier=3, max_multiplier=27)
            == 9
        )

    def test_escalation_is_capped(self):
        closed = [_closed(0, m, m) for m in range(1, 6)]  # 5 straight losses
        # 3**5 = 243, capped to 27.
        assert (
            compute_quantity_multiplier(closed, base_multiplier=3, max_multiplier=27)
            == 27
        )

    def test_unordered_input_uses_close_time(self):
        # Most recent (minute 3) is a loss regardless of list order.
        closed = [_closed(0, 3, 3), _closed(1, 1, 1), _closed(1, 2, 2)]
        assert (
            compute_quantity_multiplier(closed, base_multiplier=3, max_multiplier=27)
            == 3
        )


def _open_signal(*, bid: float, level_security: float) -> TradingSignal:
    spread = 0.0003
    return TradingSignal(
        epic="CS.D.AUDNZD.CFD.IP",
        score=0.95,
        direction="BUY",
        regression=RegressionResult(slope=0.1, intercept=bid, r_squared=0.95),
        sma_fast=bid,
        sma_slow=bid,
        roc=0.1,
        spread=spread,
        avg_spread=spread,
        position_in_range=55.0,
        levels=TradingLevels(
            bid=bid,
            offer=bid + spread,
            spread=spread,
            high=bid + 0.01,
            low=bid - 0.01,
            scope=0.02,
            average=bid,
            level_follower=level_security,
            level_win=bid + 0.01,
            level_zero=bid + spread,
            level_loose=level_security,
            level_security=level_security,
            stop_distance=1,
        ),
    )


def _open_market() -> dict:
    return {
        "instrument": {
            "name": "AUD/NZD",
            "expiry": "-",
            "currencies": [{"code": "AUD", "exchangeRate": 0.6, "isDefault": True}],
            "contractSize": "1",
        },
        "snapshot": {"marketStatus": "TRADEABLE", "scalingFactor": "10000"},
        "dealingRules": {
            "minNormalStopOrLimitDistance": {"value": 4.0, "unit": "POINTS"},
            "maxStopOrLimitDistance": {"value": 1000, "unit": "POINTS"},
            "minDealSize": {"value": 1},
        },
    }


class TestOpenPositionMultiplier:
    async def test_quantity_scales_with_multiplier(self):
        client = AsyncMock()
        db = AsyncMock()
        db.add = MagicMock()
        svc = TradingService(client=client, db_session=db, config=TradeConfig())
        signal = _open_signal(bid=1.21000, level_security=1.20550)
        client.get.side_effect = [
            _open_market(),
            {"dealStatus": "ACCEPTED", "dealId": "DEALX", "level": 1.21000},
        ]
        client.post = AsyncMock(return_value={"dealReference": "REF1"})

        pos = await svc.open_position(signal, quantity_multiplier=3)

        assert pos is not None
        # minDealSize (1) × multiplier (3) = 3.
        assert client.post.await_args.args[1]["size"] == "3"
        assert pos.quantity == 3

    async def test_default_multiplier_is_one(self):
        client = AsyncMock()
        db = AsyncMock()
        db.add = MagicMock()
        svc = TradingService(client=client, db_session=db, config=TradeConfig())
        signal = _open_signal(bid=1.21000, level_security=1.20550)
        client.get.side_effect = [
            _open_market(),
            {"dealStatus": "ACCEPTED", "dealId": "DEALX", "level": 1.21000},
        ]
        client.post = AsyncMock(return_value={"dealReference": "REF1"})

        pos = await svc.open_position(signal)

        assert pos is not None
        assert client.post.await_args.args[1]["size"] == "1"
        assert pos.quantity == 1
