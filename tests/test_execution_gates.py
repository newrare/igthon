"""Tests for the pure pre-open rules (src/execution/gates.py).

Correctness gates only (trading hours, long-only unless ``allow_short``,
duplicate-epic suppression, same-day re-open policy) plus the recovery-revert
rule — exit- and entry-agnostic, no I/O.
"""

from types import SimpleNamespace

from src.execution.gates import (
    RECOVERY_REVERT_REASON_OPEN,
    evaluate_open_gates,
    original_stop_level,
    reverse_direction,
    should_revert_after_stop_loss,
)


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


def _revert(**overrides):
    """A long stopped out at its opening stop — the canonical revert case."""
    base = {
        "direction": "BUY",
        "reason_close": "closed_externally",
        "reason_open": "auto",
        "euro": -12.5,
        "level_close": 1.0980,
        "original_stop": 1.0985,
        "stop_ratcheted": False,
    }
    base.update(overrides)
    return should_revert_after_stop_loss(**base)


class TestReverseDirection:
    def test_long_reverses_to_short(self):
        assert reverse_direction("BUY") == "SELL"

    def test_short_reverses_to_long(self):
        assert reverse_direction("SELL") == "BUY"

    def test_missing_direction_is_treated_as_long(self):
        # Position.direction defaults to BUY across the codebase.
        assert reverse_direction(None) == "SELL"


class TestOriginalStopLevel:
    """The stop placed at open is the FIRST point of the stop trajectory."""

    def test_first_history_point_wins_over_later_ratchets(self):
        position = SimpleNamespace(
            stop_history=[
                {"t": "1", "level": 1.0985, "broker": 1.0980},
                {"t": "2", "level": 1.1010, "broker": 1.1005},
            ],
            stop_update=1,
            level_follower=1.1010,
        )
        assert original_stop_level(position) == 1.0985

    def test_broker_level_used_when_software_level_absent(self):
        position = SimpleNamespace(
            stop_history=[{"t": "1", "broker": 1.0980}],
            stop_update=0,
            level_follower=0,
        )
        assert original_stop_level(position) == 1.0980

    def test_legacy_row_without_history_uses_the_unratcheted_follower(self):
        position = SimpleNamespace(
            stop_history=None, stop_update=0, level_follower=1.0985
        )
        assert original_stop_level(position) == 1.0985

    def test_legacy_row_with_a_ratcheted_stop_is_unknown(self):
        # No trajectory and the follower already moved: the level placed at open
        # is genuinely unrecoverable, so report "unknown" rather than guess.
        position = SimpleNamespace(
            stop_history=None, stop_update=3, level_follower=1.1010
        )
        assert original_stop_level(position) == 0.0


class TestRecoveryRevertRule:
    """A loss taken on the stop the position was OPENED with — and only that."""

    def test_broker_stop_out_at_a_loss_reverts(self):
        revert, reason = _revert()
        assert revert is True and reason == "OK"

    def test_software_backstop_reverts(self):
        revert, _ = _revert(reason_close="stop")
        assert revert is True

    def test_legacy_loose_close_reverts(self):
        revert, _ = _revert(reason_close="loose")
        assert revert is True

    def test_short_stopped_out_above_its_entry_reverts(self):
        revert, reason = _revert(
            direction="SELL",
            level_close=1.1015,
            original_stop=1.1010,
            stop_ratcheted=True,
        )
        assert revert is True and reason == "OK"

    def test_a_win_never_reverts(self):
        revert, reason = _revert(euro=8.0)
        assert not revert and "not a loss" in reason

    def test_break_even_close_never_reverts(self):
        revert, reason = _revert(euro=0.0)
        assert not revert and "not a loss" in reason

    def test_manual_close_never_reverts(self):
        revert, reason = _revert(reason_close="manual")
        assert not revert and "not a stop hit" in reason

    def test_end_of_day_close_never_reverts(self):
        revert, reason = _revert(reason_close="end_of_day")
        assert not revert and "not a stop hit" in reason

    def test_phantom_row_never_reverts(self):
        revert, reason = _revert(reason_close="never_opened", euro=0.0)
        assert not revert and "not a stop hit" in reason

    def test_unknown_original_stop_never_reverts(self):
        revert, reason = _revert(original_stop=0.0)
        assert not revert and "unknown" in reason

    def test_ratcheted_stop_above_the_close_does_not_revert(self):
        # The trade went the right way first and was stopped on a RAISED stop:
        # trailing logic doing its job, not a market reversal.
        revert, reason = _revert(level_close=1.1000, stop_ratcheted=True)
        assert not revert and "ratcheted" in reason

    def test_ratcheted_stop_gapped_through_the_original_level_reverts(self):
        # Price gapped past the raised stop and straight through the original
        # level — the market did walk through where the trade was built.
        revert, _ = _revert(level_close=1.0970, stop_ratcheted=True)
        assert revert is True

    def test_a_revert_is_never_reverted_again(self):
        # Single hop: a stopped-out revert does not flip back (no ping-pong).
        revert, reason = _revert(reason_open=RECOVERY_REVERT_REASON_OPEN)
        assert not revert and "no second hop" in reason
