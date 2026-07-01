"""Tests for the support-anchored initial stop close profile.

``support_atr_profit`` changes ONLY where the protective stop is placed at open:
below a recency-weighted low quantile of the last-hour bid lows, floored so it is
never tighter than the reference ATR stop. Every per-tick decision is inherited
unchanged from ``atr_trailing_profit`` — these tests cover the new initial_plan
and the ``weighted_support`` helper, plus a smoke check that the inherited
trailing still behaves.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.core.indicators import atr
from src.exit import SupportAtrProfitExit, get_close_profile
from src.exit.atr_trailing_profit import AtrTrailingProfitExit
from src.exit.base import ACTION_HOLD, CloseProfile
from src.exit.support_atr_profit import weighted_support
from src.feed.price_buffer import Candle, EpicBuffer

_START = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)


def _buffer(closes: list[float], spread: float = 0.5) -> EpicBuffer:
    buf = EpicBuffer(epic="TEST.EPIC", max_candles=len(closes) + 10)
    prev = closes[0]
    for i, close in enumerate(closes):
        high = max(prev, close) + 0.1
        low = min(prev, close) - 0.1
        buf.add(
            Candle(
                timestamp=_START + timedelta(minutes=i),
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


def _position(**overrides) -> SimpleNamespace:
    base = {
        "level_open": 8000.0,
        "level_win": 0.0,
        "level_loose": 0.0,
        "level_zero": 0.0,
        "level_follower": 0.0,
        "level_margin": 0.0,
        "euro_per_point": 0.0,
        "euro_stop": 0.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestWeightedSupport:
    def test_single_value_returns_it(self):
        assert weighted_support([123.4]) == 123.4

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            weighted_support([])

    def test_ignores_a_lone_deep_wick(self):
        # One freak low far below a wall of defended lows must NOT become the
        # support — the mass of the distribution outvotes it.
        lows = [100.0] * 59 + [50.0]  # 50.0 is the most recent, lone spike
        support = weighted_support(lows, percentile=0.10)
        assert support == 100.0

    def test_low_percentile_sits_near_the_bottom(self):
        # A genuine lower shelf (not a lone wick) is picked up at a low quantile.
        lows = [90.0] * 20 + [100.0] * 40
        assert weighted_support(lows, percentile=0.10) == 90.0

    def test_percentile_is_monotonic(self):
        lows = [float(x) for x in range(100)]
        low_q = weighted_support(lows, percentile=0.10, recency_half_life=0)
        high_q = weighted_support(lows, percentile=0.90, recency_half_life=0)
        assert low_q < high_q

    def test_recency_weighting_favours_recent_lows(self):
        # Same two shelves, but which one is recent flips the weighted support:
        # recent-low series supports lower than recent-high series.
        recent_low = [110.0] * 30 + [100.0] * 30
        recent_high = [100.0] * 30 + [110.0] * 30
        assert weighted_support(recent_low, percentile=0.30) <= weighted_support(
            recent_high, percentile=0.30
        )


class TestRegistry:
    def test_known_name_resolves(self):
        prof = get_close_profile("support_atr_profit", SimpleNamespace())
        assert isinstance(prof, SupportAtrProfitExit)

    def test_is_close_profile_instance(self):
        assert isinstance(SupportAtrProfitExit(), CloseProfile)

    def test_inherits_the_profit_profile(self):
        assert isinstance(SupportAtrProfitExit(), AtrTrailingProfitExit)


class TestInitialPlan:
    def test_floor_governs_when_support_is_close(self):
        # Flat market: the support hugs the entry, so the distance is clamped to
        # the ATR floor (never tighter than the reference).
        buf = _buffer([8000.0] * 40)
        entry = buf.last.bid_close
        atr_v = atr(list(buf.candles), 14)
        prof = SupportAtrProfitExit(min_stop_atr_k=10.0, min_stop_spread_k=0.0)
        plan = prof.initial_plan(entry_level=entry, direction="BUY", buf=buf)
        assert plan.stop_level == pytest.approx(entry - 10.0 * atr_v)

    def test_support_governs_when_it_sits_far_below(self):
        # A recovered dip leaves a defended shelf well below the entry: with the
        # floor disabled the stop lands at that support minus the ATR buffer.
        buf = _buffer([7950.0] * 20 + [8000.0] * 40)
        entry = buf.last.bid_close
        atr_v = atr(list(buf.candles), 14)
        lows = [c.bid_low for c in buf.candles]
        prof = SupportAtrProfitExit(
            min_stop_atr_k=0.0,
            min_stop_spread_k=0.0,
            stop_buffer_atr_k=0.5,
            max_stop_atr_k=0.0,  # disable the cap to isolate the support path
        )
        expected_support = weighted_support(
            lows,
            percentile=prof.support_percentile,
            recency_half_life=prof.support_recency_half_life,
        )
        plan = prof.initial_plan(entry_level=entry, direction="BUY", buf=buf)
        assert plan.stop_level == pytest.approx(expected_support - 0.5 * atr_v)
        assert plan.stop_level < entry

    def test_upper_cap_clips_a_far_support(self):
        # A far support with the cap enabled clips the distance to max_stop_atr_k
        # × ATR (the floor still wins if it is wider than the cap).
        buf = _buffer([7950.0] * 20 + [8000.0] * 40)
        entry = buf.last.bid_close
        atr_v = atr(list(buf.candles), 14)
        prof = SupportAtrProfitExit(
            min_stop_atr_k=0.0, min_stop_spread_k=0.0, max_stop_atr_k=4.0
        )
        plan = prof.initial_plan(entry_level=entry, direction="BUY", buf=buf)
        assert plan.stop_level == pytest.approx(entry - 4.0 * atr_v)

    def test_cap_never_overrides_the_floor(self):
        # A cap smaller than the floor must not tighten below the floor.
        buf = _buffer([8000.0] * 40)
        entry = buf.last.bid_close
        atr_v = atr(list(buf.candles), 14)
        prof = SupportAtrProfitExit(
            min_stop_atr_k=3.0, min_stop_spread_k=0.0, max_stop_atr_k=1.0
        )
        plan = prof.initial_plan(entry_level=entry, direction="BUY", buf=buf)
        assert plan.stop_level == pytest.approx(entry - 3.0 * atr_v)

    def test_never_tighter_than_the_reference_atr_stop(self):
        # For the same market, the support stop must sit at or below the reference
        # atr_trailing_profit stop — this profile only ever widens, never tightens.
        buf = _buffer([7950.0] * 20 + [8000.0] * 40)
        entry = buf.last.bid_close
        ref = AtrTrailingProfitExit().initial_plan(
            entry_level=entry, direction="BUY", buf=buf
        )
        got = SupportAtrProfitExit().initial_plan(
            entry_level=entry, direction="BUY", buf=buf
        )
        assert got.stop_level <= ref.stop_level + 1e-9

    def test_margin_level_is_inherited_above_break_even(self):
        # The open-frozen margin level (used by the inherited trailing) is still
        # produced by the parent and unchanged.
        buf = _buffer([8000.0 + i for i in range(40)])
        plan = SupportAtrProfitExit().initial_plan(
            entry_level=buf.last.bid_close, direction="BUY", buf=buf
        )
        assert plan.level_margin > plan.level_zero
        assert plan.profile == "support_atr_profit"

    def test_sell_keeps_the_inherited_atr_stop(self):
        # Long-only pipeline: SELL is not re-derived from support.
        buf = _buffer([8000.0 - i for i in range(40)])
        entry = buf.last.bid_close
        ref = AtrTrailingProfitExit().initial_plan(
            entry_level=entry, direction="SELL", buf=buf
        )
        got = SupportAtrProfitExit().initial_plan(
            entry_level=entry, direction="SELL", buf=buf
        )
        assert got.stop_level == pytest.approx(ref.stop_level)


class TestInheritedTrailing:
    def test_underwater_holds_without_lowering(self):
        # The inherited profit gate: below entry but above the follower → hold,
        # stop untouched. Confirms evaluate() is the parent's, unchanged.
        buf = _buffer([8000.0 + i for i in range(40)])
        pos = _position(level_open=8030.0, level_follower=8000.0)
        decision = SupportAtrProfitExit().evaluate(
            pos, current_bid=8020.0, buf=buf, is_close_hour=False
        )
        assert decision.action == ACTION_HOLD
        assert decision.new_stop_level is None
