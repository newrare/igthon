"""Tests for the per-zone stop updaters and the zone classifier.

The close profile splits per-tick stop management into four zones by where the
live bid sits relative to break-even (``level_zero``), the margin level
(``level_margin``) and the profit trigger (one further noise margin past the
margin). Zone 1 manages the risk still carried, zone 2 the delicate band just past
break-even, zone 3 secures the gain once the margin is cleared, and zone 4 runs the
momentum-gated ATR chandelier that trails the bid up in steps.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.core.indicators import atr
from src.exit.zones import (
    BreakevenBandStop,
    BreakevenHalfStop,
    BreakevenLockParams,
    BreakevenLockStop,
    BreakevenSafeStop,
    LimitLooseStop,
    SecureHoldStop,
    StopContext,
    StopZone,
    TrailingRatchetMoreStop,
    TrailingRatchetStop,
    UnderwaterStop,
    UnderwaterTrendCutStop,
    classify_zone,
)
from src.feed.price_buffer import Candle, EpicBuffer

# A confirm window larger than any test buffer disables the support-anchored
# break-even FLOOR, isolating the chandelier for the tests that target it.
_NO_FLOOR = BreakevenLockParams(confirm_window=999)

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


def _ctx(
    buf: EpicBuffer,
    *,
    current_bid,
    level_zero,
    level_margin,
    level_follower,
    direction="BUY",
):
    """A BUY context by default; ``direction="SELL"`` mirrors it (the caller then
    passes the live OFFER as ``current_bid``, since that is a short's close-out
    price)."""
    return StopContext(
        current_price=current_bid,
        level_open=level_zero,
        level_zero=level_zero,
        level_margin=level_margin,
        level_follower=level_follower,
        atr_value=atr(list(buf.candles), 14),
        spread=buf.last.spread,
        euro_per_point=0.0,
        buf=buf,
        direction=direction,
    )


class TestClassifyZone:
    # Arguments are (price, break-even, MARGIN line, PROFIT TRIGGER). Here:
    # break-even=100, margin=110 → profit trigger = 2×110 − 100 = 120. Each
    # boundary belongs to the lower zone.

    def test_at_or_below_break_even_is_underwater(self):
        assert classify_zone(99.0, 100.0, 110.0, 120.0) is StopZone.UNDERWATER
        assert classify_zone(100.0, 100.0, 110.0, 120.0) is StopZone.UNDERWATER

    def test_between_break_even_and_margin_is_the_band(self):
        assert classify_zone(105.0, 100.0, 110.0, 120.0) is StopZone.BREAKEVEN_BAND
        assert classify_zone(110.0, 100.0, 110.0, 120.0) is StopZone.BREAKEVEN_BAND

    def test_between_margin_and_profit_trigger_is_secure(self):
        # The region that used to be swallowed by the break-even band: it is now a
        # zone of its own, selected by CLOSE_ZONESECURE.
        assert classify_zone(110.1, 100.0, 110.0, 120.0) is StopZone.SECURE
        assert classify_zone(115.0, 100.0, 110.0, 120.0) is StopZone.SECURE
        assert classify_zone(120.0, 100.0, 110.0, 120.0) is StopZone.SECURE

    def test_above_profit_trigger_is_profit(self):
        assert classify_zone(120.1, 100.0, 110.0, 120.0) is StopZone.PROFIT

    def test_a_sell_mirrors_every_boundary(self):
        # Profit is down for a short: break-even=100, margin=90 → trigger 80.
        assert classify_zone(101.0, 100.0, 90.0, 80.0, -1.0) is StopZone.UNDERWATER
        assert classify_zone(95.0, 100.0, 90.0, 80.0, -1.0) is StopZone.BREAKEVEN_BAND
        assert classify_zone(85.0, 100.0, 90.0, 80.0, -1.0) is StopZone.SECURE
        assert classify_zone(79.0, 100.0, 90.0, 80.0, -1.0) is StopZone.PROFIT


class TestHoldingZones:
    def test_underwater_holds(self):
        buf = _buffer([8000.0 + i for i in range(40)])
        ctx = _ctx(
            buf,
            current_bid=7990.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        assert UnderwaterStop().propose(ctx) is None

    def test_breakeven_band_holds(self):
        buf = _buffer([8000.0 + i for i in range(40)])
        ctx = _ctx(
            buf,
            current_bid=8005.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        assert BreakevenBandStop().propose(ctx) is None


class TestUnderwaterTrendCut:
    # Initial risk R = level_zero - level_follower. With level_zero=8000 and
    # level_follower=7950, R=50, so the default cut_fraction=0.5 tightens the stop
    # to 8000 - 25 = 7975 (roughly -0.5R) once a clean adverse trend is confirmed.

    def _falling_buffer(self) -> EpicBuffer:
        # A clean, monotone downtrend since open: slope < 0 and ER ≈ 1.
        return _buffer([8000.0 - i for i in range(20)])

    def test_tightens_to_half_risk_on_a_clean_downtrend(self):
        buf = self._falling_buffer()
        ctx = _ctx(
            buf,
            current_bid=7980.0,  # underwater, still above the 7975 cut level
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        # Raised from 7950 to the -0.5R level (7975), below the live bid: it waits
        # there and cuts the loser at half the planned risk if price keeps falling.
        assert UnderwaterTrendCutStop().propose(ctx) == pytest.approx(7975.0)

    def test_parks_under_the_bid_when_price_fell_past_the_cut_level(self):
        buf = self._falling_buffer()
        ctx = _ctx(
            buf,
            current_bid=7972.0,  # already below the 7975 cut level
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        # The stop cannot sit through the market, so it is parked one spread under
        # the bid (all-but-immediate cut) rather than at the unreachable cut level.
        result = UnderwaterTrendCutStop().propose(ctx)
        assert result == pytest.approx(7972.0 - buf.last.spread)
        assert result < ctx.current_price

    def test_holds_the_wide_stop_on_a_choppy_drift(self):
        # A sawtooth that drifts down slightly: slope < 0 but ER is low (small net
        # move over a long zigzag path) — noise, not a trend, so the stop holds.
        closes = [8000.0 - 0.4 * i + (6.0 if i % 2 else -6.0) for i in range(20)]
        buf = _buffer(closes)
        ctx = _ctx(
            buf,
            current_bid=7985.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        assert UnderwaterTrendCutStop().propose(ctx) is None

    def test_holds_when_the_move_is_not_downward(self):
        buf = _buffer([7960.0 + i for i in range(20)])  # rising since open
        ctx = _ctx(
            buf,
            current_bid=7995.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        assert UnderwaterTrendCutStop().propose(ctx) is None

    def test_holds_with_too_few_ticks(self):
        buf = _buffer([8000.0 - i for i in range(10)])  # < min_ticks + 1
        ctx = _ctx(
            buf,
            current_bid=7980.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        assert UnderwaterTrendCutStop().propose(ctx) is None

    def test_does_nothing_once_the_follower_is_at_break_even(self):
        # A prior excursion already locked a level at/above break-even; there is no
        # underwater risk left to tighten, so this updater stands aside.
        buf = self._falling_buffer()
        ctx = _ctx(
            buf,
            current_bid=7990.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=8005.0,
        )
        assert UnderwaterTrendCutStop().propose(ctx) is None

    def test_never_lowers_the_stop(self):
        # Clean downtrend, but the cut level sits below the current follower — the
        # up-only guard refuses to re-post a lower stop.
        buf = self._falling_buffer()
        ctx = _ctx(
            buf,
            current_bid=7980.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7990.0,
        )
        assert UnderwaterTrendCutStop().propose(ctx) is None


class TestBreakevenLock:
    # A monotonically non-decreasing bid has zero adverse-tick-noise, so the gate
    # reduces to ``swing_low > level_zero`` and the lock lands at
    # ``level_zero + lock_fraction × (swing_low − level_zero)``. The buffers below
    # climb, then hold at a plateau so the confirmation window's swing low is a
    # known value while the bid stays inside the band (≤ level_margin).

    def _held_buffer(self, plateau: float) -> EpicBuffer:
        # Rise into a long flat plateau so ``min`` over the confirm window is the
        # plateau level and the noise band is zero (non-decreasing closes).
        rise = [7990.0, 7993.0, 7996.0, 7999.0]
        return _buffer(rise + [plateau] * 20)

    def test_locks_under_swing_low_once_move_has_held(self):
        # Plateau at 8005 held for the whole window → swing_low = 8005, noise = 0.
        # target = 8000 + 0.6 × (8005 − 8000) = 8003.0.
        buf = self._held_buffer(8005.0)
        ctx = _ctx(
            buf,
            current_bid=8005.0,  # in the band (≤ 8010), matches the plateau
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        new_stop = BreakevenLockStop().propose(ctx)
        assert new_stop == pytest.approx(8003.0)
        assert ctx.level_zero < new_stop < ctx.current_price
        assert new_stop > ctx.level_follower

    def test_floor_never_pins_the_stop_at_or_above_the_bid(self):
        # Regression: a flat plateau hugging break-even has zero adverse-tick-noise,
        # so the persistence gate opens — but the ``level_zero + spread`` sliver-lock
        # floor then lands ABOVE a bid sitting within a spread of break-even.
        # Returning it would let the close profile's software backstop close the
        # trade at ~break-even on the next tick (the "everything exits at 0 €" pin).
        # The lock must hold instead.
        buf = _buffer([7999.6, 7999.8] + [8000.3] * 20, spread=0.5)
        ctx = _ctx(
            buf,
            current_bid=8000.3,  # in the band, only 0.3 above break-even
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        # floor = level_zero + spread = 8000.5 > current_bid 8000.3 → would pin.
        assert BreakevenLockStop().propose(ctx) is None

    def test_holds_while_swing_low_has_not_cleared_break_even(self):
        # A recent dip back to/under break-even inside the window → swing_low is
        # not clear of break-even (net of noise) → the move has not held → hold.
        buf = _buffer([8005.0] * 25 + [7999.0] + [8004.0] * 9)  # dip in last 10
        ctx = _ctx(
            buf,
            current_bid=8004.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        assert BreakevenLockStop().propose(ctx) is None

    def test_holds_with_too_few_candles(self):
        # Fewer closes than the confirmation window → cannot assess persistence.
        buf = _buffer([8004.0] * 5)
        ctx = _ctx(
            buf,
            current_bid=8004.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        assert BreakevenLockStop().propose(ctx) is None

    def test_never_lowers_an_already_pushed_stop(self):
        # Follower already above the lock target (e.g. pushed by the profit zone on
        # an earlier excursion) → hold rather than pull it back down.
        buf = self._held_buffer(8005.0)
        ctx = _ctx(
            buf,
            current_bid=8005.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=8004.0,  # > 8003.0 target
        )
        assert BreakevenLockStop().propose(ctx) is None


class TestBreakevenSafe:
    # ``breakeven_safe`` raises the stop ONCE, after two consecutive rising ticks,
    # to the lower of the +10 € and +3 % (of the recent price range) references.
    # Its ctx needs a real euro-per-point, so these tests build the context
    # directly rather than via the shared ``_ctx``.

    def _ctx_eur(
        self,
        buf,
        *,
        current_bid,
        level_zero,
        level_margin,
        level_follower,
        euro_per_point,
        direction="BUY",
    ):
        return StopContext(
            current_price=current_bid,
            level_open=level_zero,
            level_zero=level_zero,
            level_margin=level_margin,
            level_follower=level_follower,
            atr_value=atr(list(buf.candles), 14),
            spread=buf.last.spread,
            euro_per_point=euro_per_point,
            buf=buf,
            direction=direction,
        )

    def test_locks_the_euro_reference_when_it_is_the_lower(self):
        # Wide range → +3 % is far (≈ +30 pts); euro_per_point = 2 → +10 € = 5 pts.
        # The lower reference (the euro lock at 8005) is taken. The early 7000 close
        # stretches the buffer range to ~1006 pts so +3 % lands at ~8030, above it.
        buf = _buffer([7000.0, 8003.0, 8004.0, 8005.0, 8006.0])  # rising tail
        ctx = self._ctx_eur(
            buf,
            current_bid=8008.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
            euro_per_point=2.0,
        )
        new_stop = BreakevenSafeStop().propose(ctx)
        assert new_stop == pytest.approx(8005.0)  # 8000 + 10 € / 2 €·pt
        assert ctx.level_zero < new_stop < ctx.current_price

    def test_locks_the_three_percent_reference_when_it_is_the_lower(self):
        # Tight range → +3 % is tiny; euro_per_point = 2 → +10 € = 5 pts is higher.
        # Range = (8006.1 high) − (8002.9 low) = 3.2 → +3 % = 8000 + 0.096 = 8000.096.
        buf = _buffer([8003.0, 8004.0, 8005.0, 8006.0])  # rising tail, ~3.2 pt range
        ctx = self._ctx_eur(
            buf,
            current_bid=8008.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
            euro_per_point=2.0,
        )
        new_stop = BreakevenSafeStop().propose(ctx)
        assert new_stop == pytest.approx(8000.096)  # 8000 + 0.03 × 3.2
        assert ctx.level_zero < new_stop < ctx.current_price

    def test_uses_the_range_reference_when_euro_per_point_is_missing(self):
        # No euro-per-point → the euro reference drops out, only +3 % remains.
        buf = _buffer([8003.0, 8004.0, 8005.0, 8006.0])
        ctx = self._ctx_eur(
            buf,
            current_bid=8008.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
            euro_per_point=0.0,
        )
        new_stop = BreakevenSafeStop().propose(ctx)
        assert new_stop == pytest.approx(8000.096)  # 8000 + 0.03 × 3.2

    def test_holds_without_a_rising_streak(self):
        # Last move is down → the two-rising-tick gate never opens.
        buf = _buffer([8005.0, 8006.0, 8007.0, 8006.5])
        ctx = self._ctx_eur(
            buf,
            current_bid=8006.5,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
            euro_per_point=2.0,
        )
        assert BreakevenSafeStop().propose(ctx) is None

    def test_holds_when_the_last_bar_is_bearish(self):
        # Close-to-close streak rises (8003 < 8004 < 8006) so the streak gate opens,
        # but the last bar gapped up and faded: it opens at 8007 and closes at 8006
        # (a down bar) while still beating the prior close. Do not raise into that
        # reversal — hold and wait for the push to resume.
        buf = EpicBuffer(epic="TEST.EPIC", max_candles=10)
        spread = 0.5
        for i, (bid_open, bid_close) in enumerate(
            [(8002.0, 8003.0), (8003.0, 8004.0), (8007.0, 8006.0)]
        ):
            high = max(bid_open, bid_close) + 0.1
            low = min(bid_open, bid_close) - 0.1
            buf.add(
                Candle(
                    timestamp=_START + timedelta(minutes=i),
                    bid_open=bid_open,
                    bid_close=bid_close,
                    bid_high=high,
                    bid_low=low,
                    offer_open=bid_open + spread,
                    offer_close=bid_close + spread,
                    offer_high=high + spread,
                    offer_low=low + spread,
                )
            )
        ctx = self._ctx_eur(
            buf,
            current_bid=8006.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
            euro_per_point=2.0,
        )
        assert BreakevenSafeStop().propose(ctx) is None

    def test_holds_with_too_few_ticks(self):
        # A single close cannot form a two-tick rising streak.
        buf = _buffer([8006.0])
        ctx = self._ctx_eur(
            buf,
            current_bid=8006.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
            euro_per_point=2.0,
        )
        assert BreakevenSafeStop().propose(ctx) is None

    def test_holds_when_the_lock_would_reach_the_bid(self):
        # Wide range → euro lock is the lower reference at 8005, but the bid sits at
        # 8004 (< 8005), so locking there would force an immediate exit → hold.
        buf = _buffer([7000.0, 8003.0, 8004.0, 8005.0, 8006.0])
        ctx = self._ctx_eur(
            buf,
            current_bid=8004.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
            euro_per_point=2.0,
        )
        assert BreakevenSafeStop().propose(ctx) is None

    def test_raises_only_once_while_in_the_margin_zone(self):
        # Follower already above break-even → the single raise has been done (or the
        # profit zone moved it); hold for the rest of the zone, never raise again.
        buf = _buffer([7000.0, 8003.0, 8004.0, 8005.0, 8006.0])
        ctx = self._ctx_eur(
            buf,
            current_bid=8008.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=8005.0,  # already above break-even
            euro_per_point=2.0,
        )
        assert BreakevenSafeStop().propose(ctx) is None


class TestLimitLoose:
    # ``limitloose`` (zone 2) brings the stop up to a DOUBLE adverse-noise band
    # under the live price the moment price clears break-even — no confirmation
    # streak, and the level may well sit short of break-even.

    def _noisy(self) -> EpicBuffer:
        """A rising tape that gives back 1.0 every other candle (measurable noise)."""
        return _buffer([8000.0 + i * 0.5 - (1.0 if i % 2 else 0.0) for i in range(30)])

    def test_parks_the_stop_two_noise_bands_under_the_price(self):
        buf = self._noisy()
        ctx = _ctx(
            buf,
            current_bid=8005.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        noise = ctx.adverse_noise(20, 2.0)
        assert noise > 0
        assert LimitLooseStop().propose(ctx) == pytest.approx(8005.0 - 2 * noise)

    def test_fires_on_the_first_tick_without_any_streak(self):
        # A single tick past break-even is enough — the two closes before it went
        # the wrong way, which every other zone-2 updater would refuse to act on.
        buf = _buffer([8006.0, 8004.0, 8003.0, 8005.0])
        ctx = _ctx(
            buf,
            current_bid=8005.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        new_stop = LimitLooseStop().propose(ctx)
        assert new_stop is not None
        assert new_stop < 8005.0

    def test_cushion_is_floored_at_the_broker_minimum(self):
        # Noise smaller than IG's minimum distance → the broker floor wins, so the
        # level is one IG would actually accept.
        buf = _buffer([8004.9, 8005.0] * 10)
        ctx = _ctx(
            buf,
            current_bid=8005.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        ctx.min_stop_distance = 3.0
        assert LimitLooseStop().propose(ctx) == pytest.approx(8002.0)

    def test_flat_tape_holds_rather_than_stopping_on_the_price(self):
        # No adverse noise, no broker minimum → no cushion at all. A stop on the
        # live price would be closed at once by the software backstop.
        buf = _buffer([8000.0 + i * 0.2 for i in range(20)])
        ctx = _ctx(
            buf,
            current_bid=8005.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        assert LimitLooseStop().propose(ctx) is None

    def test_never_loosens_an_existing_stop(self):
        buf = self._noisy()
        ctx = _ctx(
            buf,
            current_bid=8005.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=8004.9,  # already tighter than any noise-based level
        )
        assert LimitLooseStop().propose(ctx) is None

    def test_mirrors_for_a_sell(self):
        # A short's stop sits ABOVE its close-out offer, one double band away.
        buf = _buffer([8000.0 - i * 0.5 + (1.0 if i % 2 else 0.0) for i in range(30)])
        ctx = _ctx(
            buf,
            current_bid=7995.0,  # the close-out OFFER for a short
            level_zero=8000.0,
            level_margin=7990.0,
            level_follower=8050.0,
            direction="SELL",
        )
        noise = ctx.adverse_noise(20, 2.0)
        assert noise > 0
        assert LimitLooseStop().propose(ctx) == pytest.approx(7995.0 + 2 * noise)


class TestSecureHold:
    def test_hold_leaves_the_stop_alone(self):
        buf = _buffer([8000.0 + i for i in range(40)])
        ctx = _ctx(
            buf,
            current_bid=8015.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        assert SecureHoldStop().propose(ctx) is None


class TestBreakevenHalf:
    # ``breakeven_half`` (zone 3) secures the gain IMMEDIATELY, at the midpoint of
    # the break-even→margin band, as soon as price trades past the margin line.

    def test_secures_the_midpoint_on_the_first_tick(self):
        # level_zero=8000, level_margin=8010 → midpoint 8005. The bid sits in the
        # secure zone (past the 8010 margin) and there is no confirmation gate.
        buf = _buffer([8005.0, 8011.0, 8012.0, 8013.0])
        ctx = _ctx(
            buf,
            current_bid=8015.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        new_stop = BreakevenHalfStop().propose(ctx)
        assert new_stop == pytest.approx(8005.0)
        assert ctx.level_zero < new_stop < ctx.current_price

    def test_level_does_not_depend_on_the_price_history(self):
        # A tape that never rose (the position gapped into the zone) locks the very
        # same midpoint: the level is fixed by the frozen references alone.
        buf = _buffer([8014.0, 8013.0, 8012.0, 8011.0])
        ctx = _ctx(
            buf,
            current_bid=8011.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        assert BreakevenHalfStop().propose(ctx) == pytest.approx(8005.0)

    def test_holds_when_the_midpoint_would_reach_the_price(self):
        # Price back at 8004, below the 8005 midpoint: securing there would force an
        # immediate exit through the software backstop → hold.
        buf = _buffer([8005.0, 8011.0, 8012.0, 8004.0])
        ctx = _ctx(
            buf,
            current_bid=8004.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        assert BreakevenHalfStop().propose(ctx) is None

    def test_never_loosens_an_existing_stop(self):
        # The profit zone (or a manual raise) already left a tighter follower.
        buf = _buffer([8005.0, 8011.0, 8012.0, 8013.0])
        ctx = _ctx(
            buf,
            current_bid=8015.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=8006.0,
        )
        assert BreakevenHalfStop().propose(ctx) is None

    def test_holds_without_a_margin_band(self):
        # No margin frozen on the profit side (legacy row): the fraction is
        # meaningless, so nothing is proposed.
        buf = _buffer([8005.0, 8011.0, 8012.0, 8013.0])
        ctx = _ctx(
            buf,
            current_bid=8015.0,
            level_zero=8000.0,
            level_margin=8000.0,
            level_follower=7950.0,
        )
        assert BreakevenHalfStop().propose(ctx) is None

    def test_mirrors_for_a_sell(self):
        # Short: break-even 8000, margin 7990 → midpoint 7995, below break-even.
        buf = _buffer([7995.0, 7989.0, 7988.0, 7987.0])
        ctx = _ctx(
            buf,
            current_bid=7985.0,  # the close-out OFFER, past the 7990 margin
            level_zero=8000.0,
            level_margin=7990.0,
            level_follower=8050.0,
            direction="SELL",
        )
        assert BreakevenHalfStop().propose(ctx) == pytest.approx(7995.0)


class TestTrailingRatchet:
    def test_rising_bids_far_in_profit_ratchets_up(self):
        # Chandelier clear of the margin and above the lock floor → it governs.
        buf = _buffer([8000.0 + i for i in range(60)])
        atr_v = atr(list(buf.candles), 14)
        bid = buf.last.bid_close  # far above entry, rising tail
        ctx = _ctx(
            buf,
            current_bid=bid,
            level_zero=8000.0,
            level_margin=8000.0 + 1.5 * atr_v,
            level_follower=8000.0 - 2.5 * atr_v,
        )
        new_stop = TrailingRatchetStop().propose(ctx)
        assert new_stop is not None
        assert new_stop > ctx.level_follower
        assert new_stop < bid

    def test_single_spike_does_not_ratchet_the_chandelier(self):
        # A lone up-spike preceded by a down-step → only one rising step → the
        # chandelier is not tightened. With the floor disabled, the zone holds.
        closes = [8000.0 + i for i in range(40)]
        closes[-2] = closes[-3] - 5.0
        buf = _buffer(closes)
        atr_v = atr(list(buf.candles), 14)
        bid = buf.last.bid_close
        ctx = _ctx(
            buf,
            current_bid=bid,
            level_zero=8000.0,
            level_margin=8005.0,
            level_follower=8000.0 - 2.5 * atr_v,
        )
        assert TrailingRatchetStop(lock=_NO_FLOOR).propose(ctx) is None

    def test_chandelier_never_lands_in_the_dead_band(self):
        # Rising and in profit, but the trailed stop would fall at/below the margin
        # → the chandelier is suppressed. With the floor disabled, the zone holds.
        buf = _buffer([8000.0 + i for i in range(40)])
        atr_v = atr(list(buf.candles), 14)
        bid = buf.last.bid_close
        ctx = _ctx(
            buf,
            current_bid=bid,
            level_zero=8000.0,
            # Freeze the margin just above where the stop (bid - 2.5*ATR) lands.
            level_margin=bid - 2.5 * atr_v + 1.0,
            level_follower=8000.0 - 2.5 * atr_v,
        )
        assert TrailingRatchetStop(lock=_NO_FLOOR).propose(ctx) is None

    def test_floor_establishes_first_stop_when_momentum_fails(self):
        # Bid held on a flat plateau far in profit: the momentum gate fails (no
        # rising tail) so the chandelier is idle, but the move HAS held above
        # break-even → the support-anchored floor establishes the first stop. This
        # is the "no unmanaged profit zone" guarantee.
        buf = _buffer([7995.0, 7998.0] + [8050.0] * 20)  # flat plateau tail
        ctx = _ctx(
            buf,
            current_bid=8050.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        new_stop = TrailingRatchetStop().propose(ctx)
        # swing_low = 8050, noise = 0 → floor = 8000 + 0.6 × 50 = 8030.
        assert new_stop == pytest.approx(8030.0)
        assert ctx.level_follower < new_stop < ctx.current_price

    def test_floor_applies_when_chandelier_is_in_the_dead_band(self):
        # The live scenario: rising in profit, but the chandelier (bid − k·ATR)
        # would land in the dead band and is suppressed. The floor still places a
        # support-anchored stop — which may sit below the margin, safely, because
        # it is anchored under a real swing low.
        buf = _buffer([8000.0 + i for i in range(60)])
        bid = buf.last.bid_close
        ctx = _ctx(
            buf,
            current_bid=bid,
            level_zero=8000.0,
            level_margin=bid - 0.5,  # so the chandelier is always ≤ margin
            level_follower=7950.0,
        )
        new_stop = TrailingRatchetStop().propose(ctx)
        # swing_low over the last 10 closes = 8050 → floor = 8000 + 0.6 × 50 = 8030.
        assert new_stop == pytest.approx(8030.0)
        assert new_stop < ctx.level_margin  # the floor may sit below the margin

    def test_sharp_drop_holds_the_stop(self):
        # Bid ran up then fell hard from its recent high. Absent the guard the
        # lagging lock floor / chandelier would still step the stop up; the
        # sharp-drop guard holds it this tick instead. Disabling the guard
        # (window 0) makes the same raise reappear, proving it is the cause.
        buf = _buffer([8000.0 + i for i in range(60)])  # last closes ≈ 8059
        atr_v = atr(list(buf.candles), 14)
        bid = 8050.0  # several ATR below the recent high, still above the floor
        ctx = _ctx(
            buf,
            current_bid=bid,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        assert 8059.0 - bid >= 2.0 * atr_v  # precondition: this is a sharp drop
        assert TrailingRatchetStop().propose(ctx) is None
        raised = TrailingRatchetStop(drop_guard_window=0).propose(ctx)
        assert raised is not None and raised > ctx.level_follower

    def test_wider_width_pushes_stop_further_below_bid(self):
        buf = _buffer([8000.0 + i for i in range(60)])
        atr_v = atr(list(buf.candles), 14)
        bid = buf.last.bid_close
        kw = dict(
            current_bid=bid,
            level_zero=8000.0,
            level_margin=8000.0 + 1.5 * atr_v,
            level_follower=8000.0 - 2.5 * atr_v,
        )
        # Floor disabled so the comparison isolates the chandelier width.
        narrow = TrailingRatchetStop(
            atr_k_pre=2.5, atr_k_post=2.5, lock=_NO_FLOOR
        ).propose(_ctx(buf, **kw))
        wide = TrailingRatchetStop(
            atr_k_pre=3.5, atr_k_post=3.5, lock=_NO_FLOOR
        ).propose(_ctx(buf, **kw))
        assert narrow is not None and wide is not None
        assert wide < narrow  # wider width → stop further below the bid

    def test_noise_floor_pushes_stop_further_below_a_noisy_bid(self):
        # A rising-but-noisy bid (up 6 / down 3 saw-tooth, then a rising tail):
        # the candle ATR stays small while the bid jitters, so the adverse-noise
        # floor must hold the stop further below the bid than the ATR alone would.
        closes = [8000.0]
        v = 8000.0
        for i in range(36):
            v += 6.0 if i % 2 == 0 else -3.0
            closes.append(v)
        for _ in range(3):  # rising tail so the momentum gate passes
            v += 1.0
            closes.append(v)
        buf = _buffer(closes)
        bid = buf.last.bid_close
        kw = dict(
            current_bid=bid,
            level_zero=8000.0,
            level_margin=8001.0,  # low, so neither stop lands in the dead band
            level_follower=7900.0,
        )
        # Floor disabled so the comparison isolates the chandelier noise floor.
        without = TrailingRatchetStop(noise_mult=0.0, lock=_NO_FLOOR).propose(
            _ctx(buf, **kw)
        )
        with_floor = TrailingRatchetStop(noise_mult=5.0, lock=_NO_FLOOR).propose(
            _ctx(buf, **kw)
        )
        assert without is not None and with_floor is not None
        assert with_floor < without  # noise floor → stop further below the bid


class TestTrailingRatchetMore:
    """``trailing_ratchetmore``: keep a growing share of the run just made.

    Everything ``trailing_ratchet`` does, plus a give-back cap anchored on the
    position's best excursion and a trailing width that narrows as that excursion
    grows. Each mechanism is isolated by zeroing the other.
    """

    #: The parent's behaviour, reproduced by disabling both additions.
    _AS_PARENT = dict(giveback_retention=0.0, atr_k_shrink_per_atr=0.0)

    def test_disabled_additions_reproduce_the_parent(self):
        buf = _buffer([8000.0 + i for i in range(60)])
        atr_v = atr(list(buf.candles), 14)
        kw = dict(
            current_bid=buf.last.bid_close,
            level_zero=8000.0,
            level_margin=8000.0 + 1.5 * atr_v,
            level_follower=8000.0 - 2.5 * atr_v,
        )
        parent = TrailingRatchetStop().propose(_ctx(buf, **kw))
        more = TrailingRatchetMoreStop(**self._AS_PARENT).propose(_ctx(buf, **kw))
        assert more == parent

    def test_give_back_cap_locks_half_the_peak_during_a_reversal(self):
        # Ran to 8060 then fell hard to 8030: the parent's sharp-reversal guard
        # holds its stop, so the whole run can be handed back. The cap keeps half of
        # the 60-point peak — a stop at 8030 — because its anchor is the peak, not
        # the live price or a lagging swing low.
        buf = _buffer([8000.0 + i for i in range(61)])
        kw = dict(
            current_bid=8035.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        assert TrailingRatchetStop().propose(_ctx(buf, **kw)) is None
        capped = TrailingRatchetMoreStop().propose(_ctx(buf, **kw))
        assert capped == pytest.approx(8030.0)  # 8000 + 0.5 × 60

    def test_cap_scales_with_the_retained_share(self):
        buf = _buffer([8000.0 + i for i in range(61)])
        kw = dict(
            current_bid=8035.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        keep_more = TrailingRatchetMoreStop(giveback_retention=0.5).propose(
            _ctx(buf, **kw)
        )
        keep_less = TrailingRatchetMoreStop(giveback_retention=0.25).propose(
            _ctx(buf, **kw)
        )
        assert keep_less is not None and keep_more is not None
        assert keep_less < keep_more  # a smaller retained share sits further back

    def test_cap_is_not_armed_before_the_run_is_worth_it(self):
        # A noisy market whose best close is barely past break-even: the peak is no
        # run to protect (under 1 × ATR), so the cap stays out and the parent's
        # candidates — a chandelier a full 2.5 × ATR below the bid — cannot tighten.
        buf = _buffer([7995.0, 8001.0] * 19 + [7995.0, 7998.0, 8001.0])
        atr_v = atr(list(buf.candles), 14)
        ctx = _ctx(
            buf,
            current_bid=8001.0,
            level_zero=8000.0,
            level_margin=8000.5,
            level_follower=7990.0,
        )
        peak = max(ctx.favourable_closes) - 8000.0  # 1 point
        assert peak < atr_v  # precondition: below the 1 × ATR arming threshold
        assert TrailingRatchetMoreStop(lock=_NO_FLOOR).propose(ctx) is None

    def test_cap_never_lands_at_or_past_the_live_price(self):
        # Price has collapsed back below half the peak: the cap level would sit
        # ahead of the market (an instant close), so it is dropped.
        buf = _buffer([8000.0 + i for i in range(61)])
        ctx = _ctx(
            buf,
            current_bid=8010.0,  # half the 60-point peak = 8030, ahead of price
            level_zero=8000.0,
            level_margin=8005.0,
            level_follower=7950.0,
        )
        assert TrailingRatchetMoreStop(lock=_NO_FLOOR).propose(ctx) is None

    def test_cap_never_lands_in_the_dead_band(self):
        # Half the peak falls short of the margin → the cap is suppressed, exactly
        # as the parent suppresses a chandelier there.
        buf = _buffer([8000.0 + i for i in range(61)])
        ctx = _ctx(
            buf,
            current_bid=8035.0,
            level_zero=8000.0,
            level_margin=8040.0,  # above the 8030 cap level
            level_follower=7950.0,
        )
        assert TrailingRatchetMoreStop(lock=_NO_FLOOR).propose(ctx) is None

    def test_cap_never_loosens_the_stop(self):
        # The persisted follower already sits further into profit than the cap.
        buf = _buffer([8000.0 + i for i in range(61)])
        ctx = _ctx(
            buf,
            current_bid=8035.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=8032.0,  # beyond the 8030 cap level
        )
        assert TrailingRatchetMoreStop(lock=_NO_FLOOR).propose(ctx) is None

    def test_width_narrows_as_the_run_extends(self):
        # Same rising curve, same tick: narrowing the width as the peak grows puts
        # the chandelier closer to the bid than the parent's constant 2.5 × ATR.
        buf = _buffer([8000.0 + 2.0 * i for i in range(60)])
        atr_v = atr(list(buf.candles), 14)
        kw = dict(
            current_bid=buf.last.bid_close,
            level_zero=8000.0,
            level_margin=8000.0 + 1.5 * atr_v,
            level_follower=8000.0 - 2.5 * atr_v,
        )
        # Floor and cap disabled so the comparison isolates the chandelier width.
        parent = TrailingRatchetStop(lock=_NO_FLOOR).propose(_ctx(buf, **kw))
        narrowed = TrailingRatchetMoreStop(
            lock=_NO_FLOOR, giveback_retention=0.0
        ).propose(_ctx(buf, **kw))
        assert parent is not None and narrowed is not None
        assert narrowed > parent  # tighter trail → stop closer to the bid

    def test_width_never_narrows_past_its_floor(self):
        # A very extended run: the shrink is clamped at ``atr_k_floor``, so the stop
        # stays a full floor-width behind the bid instead of hugging it.
        buf = _buffer([8000.0 + 10.0 * i for i in range(60)])
        atr_v = atr(list(buf.candles), 14)
        bid = buf.last.bid_close
        ctx = _ctx(
            buf,
            current_bid=bid,
            level_zero=8000.0,
            level_margin=8000.0 + 1.5 * atr_v,
            level_follower=8000.0 - 2.5 * atr_v,
        )
        updater = TrailingRatchetMoreStop(
            lock=_NO_FLOOR, giveback_retention=0.0, atr_k_floor=1.2
        )
        stop = updater.propose(ctx)
        assert stop is not None
        # The trailing distance is floored at 1.2 × ATR (the spread / noise floors
        # can only widen it), so the stop cannot sit closer than that to the bid.
        assert bid - stop >= 1.2 * atr_v

    def test_mirrors_for_a_sell(self):
        # Short: the close-out offer fell from 8000.5 to 7940.5 (a 59.5-point run
        # from the 8000 break-even), then bounced to 7965. Half of that run is
        # 29.75, so the cap sits 29.75 ABOVE break-even — the mirror of a long.
        buf = _buffer([8000.0 - i for i in range(61)])
        ctx = _ctx(
            buf,
            current_bid=7965.0,  # the close-out OFFER
            level_zero=8000.0,
            level_margin=7990.0,
            level_follower=8050.0,
            direction="SELL",
        )
        capped = TrailingRatchetMoreStop().propose(ctx)
        assert capped == pytest.approx(7970.25)
