"""Tests for the trading service."""

from src.services.compute import RegressionResult, TradingLevels, TradingSignal


class TestTradingSignalStructure:
    """Verify TradingSignal dataclass integrity."""

    def test_buy_signal(self):
        signal = TradingSignal(
            epic="IX.D.DAX.IFMM.IP",
            score=0.85,
            direction="BUY",
            regression=RegressionResult(slope=0.5, intercept=100.0, r_squared=0.85),
            sma_fast=25240.0,
            sma_slow=25220.0,
            roc=0.5,
            spread=1.8,
            avg_spread=1.8,
            position_in_range=55.0,
            levels=TradingLevels(
                bid=25240.0,
                offer=25241.8,
                spread=1.8,
                high=25280.0,
                low=25200.0,
                scope=80.0,
                average=25235.0,
                level_follower=25234.6,
                level_win=25247.2,
                level_zero=25241.8,
                level_loose=25225.6,
                level_security=25216.6,
                stop_distance=26,
            ),
        )
        assert signal.direction == "BUY"
        assert signal.score > 0.75
        assert signal.levels.stop_distance > 0
        assert signal.levels.level_win > signal.levels.bid
        assert signal.levels.level_loose < signal.levels.bid
