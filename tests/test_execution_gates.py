"""Tests for the pure pre-open rules (src/execution/gates.py).

Correctness gates only (trading hours, long-only unless ``allow_short``,
duplicate-epic suppression, same-day re-open policy) — exit- and entry-agnostic,
no I/O.
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

    def test_sell_blocks_when_short_not_allowed(self):
        allowed, reason = _gate(direction="SELL")
        assert not allowed and "SELL" in reason

    def test_sell_allowed_with_allow_short(self):
        allowed, reason = _gate(direction="SELL", allow_short=True)
        assert allowed is True and reason == "OK"

    def test_unknown_direction_blocks(self):
        allowed, reason = _gate(direction="HOLD", allow_short=True)
        assert not allowed and "Unknown direction" in reason

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


class TestSameDayReopenGate:
    """Global ``ALLOW_SAME_DAY_REOPEN`` policy, resolved by the caller.

    The caller passes ``epic_traded_today=True`` only when the epic was used
    today *and* the policy forbids re-using it, so the gate itself is a plain
    boolean check — direction-agnostic (BUY and SELL count the same).
    """

    def test_traded_today_blocks(self):
        allowed, reason = _gate(epic_traded_today=True)
        assert not allowed and "already traded today" in reason

    def test_traded_today_blocks_a_sell_too(self):
        allowed, reason = _gate(
            direction="SELL", allow_short=True, epic_traded_today=True
        )
        assert not allowed and "already traded today" in reason

    def test_traded_today_defaults_to_allowed(self):
        # Callers that allow same-day re-opens simply omit the flag.
        allowed, reason = _gate()
        assert allowed is True and reason == "OK"

    def test_concurrent_duplicate_reported_before_same_day(self):
        # A still-open epic is "already open", the more precise reason.
        allowed, reason = _gate(epic_already_open=True, epic_traded_today=True)
        assert not allowed and "already open" in reason
