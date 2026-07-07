"""Tests for the pure pre-open rules (src/execution/gates.py).

Correctness gates only (trading hours, BUY direction, duplicate-epic
suppression) — exit- and entry-agnostic, no I/O.
"""

from src.execution.gates import evaluate_open_gates


def _gate(**overrides):
    base = {
        "epic": "X",
        "direction": "BUY",
        "in_trading_hours": True,
        "epic_already_open": False,
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
        allowed, reason = _gate(epic_already_open=True)
        assert not allowed and "already open" in reason

    def test_closes_soon_blocks(self):
        allowed, reason = _gate(closes_soon=True)
        assert not allowed and "closes soon" in reason

    def test_closes_soon_defaults_to_allowed(self):
        # Omitting ``closes_soon`` (e.g. the simulator) must not block.
        allowed, reason = _gate()
        assert allowed is True and reason == "OK"
