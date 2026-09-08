"""Tests for the touch watcher — graze versus real turn on a follower touch.

The rule is called only once the close-out price has reached the follower, and
answers one question: close now, or give the market one minute to disown the
touch? Three bounds are load-bearing and each is isolated here — the watch only
protects an acquired gain, the give-back is capped, and a minute close beyond the
follower ends it.
"""

import pytest

from src.exit.stop_watch import TouchWatchParams, touch_closes_position, watch_band

# 10 points of locked gain, a 1-point noise band: the band is then bounded by
# 0.25 × 10 = 2.5 (gain cap), 0.5 × 1 = 0.5 (noise) and 0.5 + 1 = 1.5 (broker gap)
# → 0.5. Numbers are chosen so each cap can be made to win in turn.
_BASE = dict(
    level_follower=8010.0,
    level_zero=8000.0,
    spread=0.5,
    min_stop_distance=0.0,
    noise=1.0,
    sign=1.0,
    params=TouchWatchParams(),
)


def _band(**overrides) -> float:
    return watch_band(**{**_BASE, **overrides})


def _closes(**overrides) -> bool:
    kw = {**_BASE, "price": 8010.0, "last_close_out": 8012.0, **overrides}
    return touch_closes_position(**kw)


class TestWatchBand:
    def test_noise_sets_the_band(self):
        assert _band() == pytest.approx(0.5)  # 0.5 × noise, the tightest bound

    def test_gain_cap_wins_on_a_thin_gain(self):
        # 1.6 points locked (just past the 1 × noise gate): the watch may risk
        # 0.25 × 1.6 = 0.4, tighter than the 0.5 × 1 = 0.5 the noise would allow.
        assert _band(level_follower=8001.6) == pytest.approx(0.4)

    def test_broker_gap_caps_a_wide_band(self):
        # The gap is one spread plus the noise band (0.5 + 1 = 1.5), so it can only
        # bind a configuration asking for more than one noise band: k=2 wants 2.0
        # and is pulled back to 1.5 — watching past the order resting at IG would
        # let the broker fill first, and deeper.
        wide = TouchWatchParams(band_noise_k=2.0)
        assert _band(params=wide) == pytest.approx(1.5)

    def test_min_stop_distance_widens_the_broker_gap(self):
        # IG's floor replaces the noise inside the gap when it is wider, so the gap
        # stops binding: 0.5 + 5 = 5.5 leaves the k=2 band (2.0) intact.
        wide = TouchWatchParams(band_noise_k=2.0)
        assert _band(params=wide, min_stop_distance=5.0) == pytest.approx(2.0)

    def test_no_watch_when_the_follower_locks_too_little(self):
        # 0.5 point locked against a 1-point noise band: nothing acquired worth
        # risking (min_gain_noise = 1) → no watch at all.
        assert _band(level_follower=8000.5) == 0.0

    def test_no_watch_below_break_even(self):
        assert _band(level_follower=7990.0) == 0.0

    def test_no_watch_without_measurable_noise(self):
        assert _band(noise=0.0) == 0.0

    def test_disabled_by_a_zero_multiplier(self):
        assert _band(params=TouchWatchParams(band_noise_k=0.0)) == 0.0

    def test_mirrors_for_a_sell(self):
        # A short's follower sits ABOVE break-even in price terms; the locked gain
        # and the band are identical magnitudes.
        assert _band(level_follower=7990.0, sign=-1.0) == pytest.approx(0.5)


class TestTouchClosesPosition:
    def test_graze_inside_the_band_is_held(self):
        # 0.3 point beyond the follower (band 0.5) and the last minute closed above
        # it → the touch is a graze, the position is held.
        assert _closes(price=8009.7) is False

    def test_deep_break_closes_at_once(self):
        # 0.8 point beyond the follower, past the 0.5 band → a real move.
        assert _closes(price=8009.2) is True

    def test_a_minute_close_beyond_the_follower_ends_the_watch(self):
        # Still inside the band, but the last completed candle closed below the
        # follower: a graze does not survive a minute close.
        assert _closes(price=8009.7, last_close_out=8009.8) is True

    def test_exactly_on_the_follower_is_held(self):
        # Depth 0 — the touch that would have closed the position before.
        assert _closes(price=8010.0) is False

    def test_no_watch_closes_immediately(self):
        assert _closes(price=8009.9, level_follower=8000.5) is True
        assert _closes(price=8009.9, params=TouchWatchParams(band_noise_k=0.0)) is True

    def test_mirrors_for_a_sell(self):
        # Short: break-even 8000, follower 7990 (10 points locked), close-out offer
        # rising through it. 0.3 above → graze; 0.8 above → break.
        sell = dict(level_follower=7990.0, sign=-1.0, last_close_out=7988.0)
        assert _closes(price=7990.3, **sell) is False
        assert _closes(price=7990.8, **sell) is True
        assert _closes(price=7990.3, **{**sell, "last_close_out": 7990.2}) is True
