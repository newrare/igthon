"""Tests for the pure pre-open rules (src/execution/gates.py).

Correctness gates (trading hours, long-only unless ``allow_short``,
duplicate-epic suppression, same-day re-open policy), the spread budget, the
same-bet gate, plus the recovery-revert rule — the bookkeeping half (which stop
fired) and the curve half (did the market really break through it). Exit- and
entry-agnostic, no I/O.
"""

import random
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace

import pytest

from src.execution import gates
from src.execution.gates import (
    MAX_SPREAD_STOP_RATIO,
    RECOVERY_REVERT_REASON_OPEN,
    curve_supports_revert,
    evaluate_open_gates,
    original_stop_level,
    position_opened_at,
    reverse_direction,
    same_bet_free,
    should_revert_after_stop_loss,
    spread_fits_stop,
    worst_case_stop_distance,
)
from src.feed.price_buffer import Candle


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


class TestWorstCaseStopDistance:
    def test_buy_runs_from_the_offer_to_the_broker_stop(self):
        # A long fills at the offer (100.5) and is stopped out on the bid at 95.
        assert worst_case_stop_distance(
            "BUY", bid=100.0, offer=100.5, broker_stop=95.0
        ) == pytest.approx(5.5)

    def test_sell_runs_from_the_bid_to_the_broker_stop(self):
        # A short fills at the bid (100) and is stopped out on the offer at 105.
        assert worst_case_stop_distance(
            "SELL", bid=100.0, offer=100.5, broker_stop=105.0
        ) == pytest.approx(5.0)


class TestSpreadBudgetGate:
    def test_small_spread_passes(self):
        assert spread_fits_stop(0.5, 10.0) == (True, "OK")

    def test_spread_at_the_limit_is_refused(self):
        allowed, reason = spread_fits_stop(MAX_SPREAD_STOP_RATIO * 10.0, 10.0)
        assert allowed is False
        assert reason.startswith("Spread budget exceeded")

    def test_reason_reports_the_measured_ratio(self):
        _, reason = spread_fits_stop(3.0, 10.0)
        assert "30%" in reason and "max 20%" in reason

    def test_no_stop_distance_is_refused(self):
        allowed, reason = spread_fits_stop(0.1, 0.0)
        assert allowed is False
        assert "no stop distance" in reason

    def test_explicit_ratio_overrides_the_constant(self):
        assert spread_fits_stop(3.0, 10.0, max_ratio=0.5)[0] is True

    def test_constant_is_read_at_call_time(self, monkeypatch):
        # Lets a backtest relax the gate without threading a parameter through.
        monkeypatch.setattr(gates, "MAX_SPREAD_STOP_RATIO", float("inf"))
        assert spread_fits_stop(9.0, 10.0)[0] is True


def _walk(seed: int, n: int = 80) -> list[float]:
    """Deterministic random-walk bid path (relative steps of ~0.1 %)."""
    rng = random.Random(seed)
    prices = [100.0]
    for _ in range(n - 1):
        prices.append(prices[-1] * (1 + rng.gauss(0, 0.001)))
    return prices


def _candles(prices: list[float], start: datetime | None = None) -> list[Candle]:
    """One-minute candles on ``prices`` (bid), stamped from ``start``."""
    t0 = start or datetime(2026, 7, 7, 10, 0, tzinfo=UTC)
    return [
        Candle(
            timestamp=t0 + timedelta(minutes=i),
            bid_open=p,
            bid_close=p,
            bid_high=p,
            bid_low=p,
            offer_open=p + 0.01,
            offer_close=p + 0.01,
            offer_high=p + 0.01,
            offer_low=p + 0.01,
        )
        for i, p in enumerate(prices)
    ]


class TestSameBetGate:
    def test_twin_listing_same_side_is_refused(self):
        # Cocoa NY + cocoa London: same curve shape at another price scale.
        path = _walk(1)
        twin = [p * 1.7 for p in path]
        allowed, reason = same_bet_free(
            epic="LCC",
            direction="SELL",
            candles=_candles(twin),
            book=[("CC", "SELL", _candles(path))],
        )
        assert allowed is False
        assert reason.startswith("Same bet as an open position")
        assert "SELL CC" in reason

    def test_twin_listing_opposite_side_is_a_hedge(self):
        path = _walk(1)
        allowed, _ = same_bet_free(
            epic="LCC",
            direction="BUY",
            candles=_candles([p * 1.7 for p in path]),
            book=[("CC", "SELL", _candles(path))],
        )
        assert allowed is True

    def test_mirrored_pair_opposite_side_is_refused(self):
        # EUR/USD long + USD/CHF short: anti-correlated curves, one dollar bet.
        path = _walk(2)
        mirror = [200.0 - p for p in path]
        allowed, _ = same_bet_free(
            epic="USDCHF",
            direction="SELL",
            candles=_candles(mirror),
            book=[("EURUSD", "BUY", _candles(path))],
        )
        assert allowed is False

    def test_independent_market_passes(self):
        allowed, _ = same_bet_free(
            epic="B",
            direction="BUY",
            candles=_candles(_walk(3)),
            book=[("A", "BUY", _candles(_walk(4)))],
        )
        assert allowed is True

    def test_own_epic_is_left_to_the_duplicate_epic_gate(self):
        path = _candles(_walk(5))
        assert same_bet_free(
            epic="A", direction="BUY", candles=path, book=[("A", "BUY", path)]
        ) == (True, "OK")

    def test_curves_without_shared_timestamps_cannot_refuse(self):
        # Another market recorded on another day: nothing to correlate.
        allowed, _ = same_bet_free(
            epic="B",
            direction="BUY",
            candles=_candles(_walk(6)),
            book=[("A", "BUY", _candles(_walk(7), datetime(2026, 7, 8, tzinfo=UTC)))],
        )
        assert allowed is True

    def test_identical_fingerprint_needs_no_shared_timestamps(self):
        # The exact same quantised path is one curve by construction.
        path = _walk(6)
        allowed, _ = same_bet_free(
            epic="B",
            direction="BUY",
            candles=_candles(path),
            book=[
                (
                    "A",
                    "BUY",
                    _candles([p * 2 for p in path], datetime(2026, 7, 8, tzinfo=UTC)),
                )
            ],
        )
        assert allowed is False

    def test_empty_book_passes(self):
        assert same_bet_free(
            epic="A", direction="BUY", candles=_candles(_walk(8)), book=[]
        ) == (True, "OK")

    def test_constant_is_read_at_call_time(self, monkeypatch):
        monkeypatch.setattr(gates, "MAX_SAME_BET_REDUNDANCY", 1.0)
        path = _walk(9)
        noisy = [p * (1 + 0.0002 * ((i % 3) - 1)) for i, p in enumerate(path)]
        allowed, _ = same_bet_free(
            epic="B",
            direction="BUY",
            candles=_candles(noisy),
            book=[("A", "BUY", _candles(path))],
        )
        assert allowed is True


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


def _curve(**overrides):
    """A clean 5-minute break through a long's opening stop.

    Level 1.10000 opened with a 1.09850 stop (risk = 0.00150), price walking
    straight down one candle per minute and still 0.00050 past the stop when the
    revert is decided.
    """
    base = {
        "direction": "BUY",
        "level_open": 1.10000,
        "original_stop": 1.09850,
        "prices": [1.09960, 1.09920, 1.09880, 1.09840, 1.09800],
        "current_price": 1.09800,
        "minutes_held": 5.5,
    }
    base.update(overrides)
    return curve_supports_revert(**base)


class TestPositionOpenedAt:
    """``date`` + naive-UTC ``time_open`` back into a UTC instant."""

    def test_columns_are_combined_as_utc(self):
        position = SimpleNamespace(date=date(2026, 8, 3), time_open=time(9, 59, 30))
        assert position_opened_at(position) == datetime(
            2026, 8, 3, 9, 59, 30, tzinfo=UTC
        )

    def test_missing_time_is_unknown(self):
        assert position_opened_at(SimpleNamespace(date=date(2026, 8, 3))) is None

    def test_missing_date_is_unknown(self):
        assert position_opened_at(SimpleNamespace(time_open=time(9, 0))) is None


class TestRevertCurveFilter:
    """Only a market that really moved deserves the opposite side.

    The bookkeeping rule above establishes that the stop placed at open is what
    fired; this one looks at the curve walked since that open, and is deliberately
    permissive: a stop-out normally *does* revert, and only the blatant "nothing
    happened" curves are dropped. What it looks for is a move **concentrated** in
    one or two candles — never how long the position was held, since a flat market
    can break in a single candle after twenty quiet minutes.
    """

    def test_clean_break_supports_the_revert(self):
        supported, reason = _curve()
        assert supported is True and reason == "OK"

    def test_mirror_break_on_a_short_supports_the_revert(self):
        supported, reason = _curve(
            direction="SELL",
            original_stop=1.10150,
            prices=[1.10040, 1.10080, 1.10120, 1.10160, 1.10200],
            current_price=1.10200,
        )
        assert supported is True and reason == "OK"

    def test_flat_range_broken_by_one_candle_supports_the_revert(self):
        # Fifteen minutes inside a 0.00010 band, then one candle straight through
        # the stop. Holding time says "flat", the impulse says "break" — and the
        # break is what matters: this is a prime revert.
        flat = [1.09990 + 0.00005 * (i % 3) for i in range(15)] + [1.09800]
        supported, reason = _curve(prices=flat, minutes_held=16)
        assert supported is True and reason == "OK"

    def test_slow_leak_to_the_stop_is_refused(self):
        # Same destination reached in forty tiny steps: no candle carries the move,
        # so there is nothing to ride on the other side.
        supported, reason = _curve(prices=[1.10000 - 0.00005 * i for i in range(1, 41)])
        assert not supported and "flat curve leaking to the stop" in reason

    def test_holding_time_alone_never_refuses(self):
        # An hour of position life with a real break in it is still a break.
        supported, reason = _curve(minutes_held=60)
        assert supported is True and reason == "OK"

    def test_noise_that_only_taps_the_stop_is_refused(self):
        # Flat band drifting just far enough to tap the stop, price back inside it
        # when the revert is decided.
        flat = [1.09990, 1.09960, 1.09985, 1.09950, 1.09980, 1.09880, 1.09860]
        supported, reason = _curve(prices=flat, current_price=1.09865, minutes_held=7)
        assert not supported and "grazed" in reason

    def test_oscillating_curve_is_refused(self):
        supported, reason = _curve(
            prices=[
                1.09990,
                1.09930,
                1.09990,
                1.09920,
                1.09980,
                1.09910,
                1.09970,
                1.09900,
                1.09960,
                1.09890,
                1.09950,
                1.09800,
            ],
            minutes_held=12,
        )
        assert not supported and "chopped its way to the stop" in reason

    def test_full_risk_in_profit_first_is_refused(self):
        # The trade drifted a full risk into PROFIT, then one candle took it back
        # through its opening stop: a 2-risk round trip is an oscillation, and the
        # direction was right at least once.
        supported, reason = _curve(
            prices=[
                1.10013,
                1.10027,
                1.10040,
                1.10053,
                1.10067,
                1.10080,
                1.10093,
                1.10107,
                1.10120,
                1.10133,
                1.10147,
                1.10160,
                1.09800,
            ],
            minutes_held=13,
        )
        assert not supported and "oscillation" in reason

    def test_grazed_stop_is_refused(self):
        # A wick took the stop out and price is already back above it.
        supported, reason = _curve(current_price=1.09900)
        assert not supported and "grazed" in reason

    def test_price_barely_past_the_stop_is_refused(self):
        # Less than 10% of the risk beyond the stop: not a break yet.
        supported, reason = _curve(current_price=1.09845)
        assert not supported and "grazed" in reason

    def test_two_candle_break_is_judged_normally(self):
        # One step is enough for the impulse and straightness tests.
        supported, reason = _curve(prices=[1.09900, 1.09800], minutes_held=2)
        assert supported is True and reason == "OK"

    def test_stop_out_before_the_first_candle_is_accepted(self):
        # The position died inside a minute: there is no curve to read, and a
        # stop-out that fast is a break by definition.
        supported, reason = _curve(prices=[1.09800], minutes_held=1)
        assert supported is True and "stopped out within" in reason

    def test_missing_candles_over_a_long_hold_are_refused(self):
        # Same single candle, but the position was held 10 minutes: the curve is
        # unknown (feed gap), and an unknown curve is not a licence to revert.
        supported, reason = _curve(prices=[1.09800], minutes_held=10)
        assert not supported and "curve unknown" in reason

    def test_unknown_risk_span_is_refused(self):
        supported, reason = _curve(original_stop=1.10000)
        assert not supported and "Risk span" in reason

    def test_missing_live_price_is_refused(self):
        supported, reason = _curve(current_price=0.0)
        assert not supported and "No live price" in reason
