"""Tests for the per-zone stop updaters and the zone classifier.

The close profile splits per-tick stop management into three zones by where the
live bid sits relative to break-even (``level_zero``) and the margin level
(``level_margin``). Zones 1 and 2 hold the stop; zone 3 runs the momentum-gated
ATR chandelier that trails the bid up in steps.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.core.indicators import atr
from src.exit.zones import (
    BreakevenBandStop,
    BreakevenLockParams,
    BreakevenLockStop,
    StopContext,
    StopZone,
    TrailingRatchetStop,
    UnderwaterStop,
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


def _ctx(buf: EpicBuffer, *, current_bid, level_zero, level_margin, level_follower):
    return StopContext(
        current_bid=current_bid,
        level_open=level_zero,
        level_zero=level_zero,
        level_margin=level_margin,
        level_follower=level_follower,
        atr_value=atr(list(buf.candles), 14),
        spread=buf.last.spread,
        euro_per_point=0.0,
        buf=buf,
    )


class TestClassifyZone:
    def test_at_or_below_break_even_is_underwater(self):
        assert classify_zone(99.0, 100.0, 110.0) is StopZone.UNDERWATER
        assert classify_zone(100.0, 100.0, 110.0) is StopZone.UNDERWATER

    def test_between_break_even_and_margin_is_band(self):
        assert classify_zone(105.0, 100.0, 110.0) is StopZone.BREAKEVEN_BAND
        assert classify_zone(110.0, 100.0, 110.0) is StopZone.BREAKEVEN_BAND

    def test_above_margin_is_profit(self):
        assert classify_zone(110.1, 100.0, 110.0) is StopZone.PROFIT


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
        assert ctx.level_zero < new_stop < ctx.current_bid
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
            level_margin=8000.0 + max(0.5 * atr_v, buf.last.spread * 2.0),
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
        assert ctx.level_follower < new_stop < ctx.current_bid

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
            level_margin=8000.0 + max(0.5 * atr_v, buf.last.spread * 2.0),
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
