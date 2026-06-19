"""Tests for the pure execution-risk rules (src/execution/risk.py).

Portfolio/risk gates and martingale sizing — exit- and entry-agnostic, no I/O.
"""

from datetime import time
from types import SimpleNamespace

from src.execution.risk import compute_quantity_multiplier, evaluate_open_gates


def _config(**overrides) -> SimpleNamespace:
    base = {
        "max_positions": 4,
        "day_euro_finish_loose": -500.0,
        "day_euro_finish_win": 300.0,
        "max_trades_day": 50,
        "min_win_rate": 0.40,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _gate(**overrides):
    base = {
        "epic": "X",
        "direction": "BUY",
        "in_trading_hours": True,
        "epic_already_open": False,
        "open_count": 0,
        "daily_pnl": 0.0,
        "trade_count": 0,
        "win_rate": 1.0,
        "config": _config(),
    }
    base.update(overrides)
    return evaluate_open_gates(**base)


class TestOpenGates:
    def test_clean_state_allows(self):
        allowed, reason = _gate()
        assert allowed is True
        assert reason == "OK"

    def test_outside_hours_blocks(self):
        allowed, reason = _gate(in_trading_hours=False)
        assert not allowed and "hours" in reason

    def test_non_buy_blocks(self):
        allowed, reason = _gate(direction="SELL")
        assert not allowed and "SELL" in reason

    def test_duplicate_epic_blocks(self):
        allowed, _ = _gate(epic_already_open=True)
        assert not allowed

    def test_max_positions_blocks(self):
        allowed, _ = _gate(open_count=4)
        assert not allowed

    def test_daily_loss_limit_blocks(self):
        allowed, reason = _gate(daily_pnl=-500.0)
        assert not allowed and "loss" in reason

    def test_daily_target_blocks(self):
        allowed, reason = _gate(daily_pnl=300.0)
        assert not allowed and "target" in reason

    def test_max_trades_blocks(self):
        allowed, _ = _gate(trade_count=50)
        assert not allowed

    def test_low_win_rate_blocks_only_after_ten_trades(self):
        assert _gate(trade_count=9, win_rate=0.0)[0] is True
        assert _gate(trade_count=12, win_rate=0.1)[0] is False


class TestQuantityMultiplier:
    @staticmethod
    def _closed(wins: list[int]):
        # Build closed positions in chronological order; tail is most recent.
        return [
            SimpleNamespace(win=w, time_close=time(10, i), time_open=time(9, i), id=i)
            for i, w in enumerate(wins)
        ]

    def test_no_history_is_one(self):
        assert (
            compute_quantity_multiplier([], base_multiplier=3, max_multiplier=27) == 1
        )

    def test_last_win_resets_to_one(self):
        closed = self._closed([0, 0, 1])
        assert (
            compute_quantity_multiplier(closed, base_multiplier=3, max_multiplier=27)
            == 1
        )

    def test_consecutive_losses_compound(self):
        closed = self._closed([1, 0, 0])  # two trailing losses -> 3**2 = 9
        assert (
            compute_quantity_multiplier(closed, base_multiplier=3, max_multiplier=27)
            == 9
        )

    def test_cap_is_respected(self):
        closed = self._closed([0, 0, 0, 0])  # 3**4 = 81 capped to 27
        assert (
            compute_quantity_multiplier(closed, base_multiplier=3, max_multiplier=27)
            == 27
        )
