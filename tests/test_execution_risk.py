"""Tests for the pure execution-risk rules (src/execution/risk.py).

Portfolio/risk gates and martingale sizing — exit- and entry-agnostic, no I/O.
"""

from datetime import time
from types import SimpleNamespace

from src.execution.risk import (
    compute_quantity_multiplier,
    daily_risk_block,
    evaluate_open_gates,
)


def _config(**overrides) -> SimpleNamespace:
    base = {
        "max_positions": 4,
        "day_euro_finish_loose": -500.0,
        "day_euro_finish_win": 300.0,
        "max_trades_day": 50,
        "min_win_rate": 0.40,
        "daily_risk_enabled": True,
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

    def test_disabled_daily_risk_bypasses_all_daily_breakers(self):
        # With the safety disarmed, daily loss / target / trade-count / win-rate
        # gates are skipped — the bot keeps opening (dev/test).
        cfg = _config(daily_risk_enabled=False)
        assert _gate(config=cfg, daily_pnl=-9999.0)[0] is True
        assert _gate(config=cfg, trade_count=999)[0] is True
        assert _gate(config=cfg, trade_count=20, win_rate=0.0)[0] is True

    def test_disabled_daily_risk_still_enforces_per_epic_gates(self):
        # Per-epic limits are NOT daily-risk; they must still block.
        cfg = _config(daily_risk_enabled=False)
        assert _gate(config=cfg, epic_already_open=True)[0] is False
        assert _gate(config=cfg, open_count=4)[0] is False


class TestDailyRiskBlock:
    """The day-scope circuit-breakers the dashboard surfaces as the Opening badge.

    Must stay in lockstep with ``evaluate_open_gates`` (which delegates to it):
    same thresholds, same reason strings — and it must ignore the per-epic gates
    (duplicate epic, max positions), which are not daily-risk reasons.
    """

    def _block(self, **overrides):
        base = {
            "daily_pnl": 0.0,
            "trade_count": 0,
            "win_rate": 1.0,
            "config": _config(),
        }
        base.update(overrides)
        return daily_risk_block(**base)

    def test_clean_state_is_none(self):
        assert self._block() is None

    def test_daily_loss_limit(self):
        assert "loss" in self._block(daily_pnl=-500.0)

    def test_daily_target(self):
        assert "target" in self._block(daily_pnl=300.0)

    def test_max_trades(self):
        assert "Max daily trades" in self._block(trade_count=50)

    def test_win_rate_floor_only_after_ten_trades(self):
        assert self._block(trade_count=9, win_rate=0.0) is None
        assert "Win rate" in self._block(trade_count=12, win_rate=0.1)

    def test_matches_evaluate_open_gates_reason(self):
        # The badge string and the live gate's refusal reason are identical.
        assert self._block(daily_pnl=-500.0) == _gate(daily_pnl=-500.0)[1]


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
