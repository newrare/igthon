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
    BreakevenHalfStop,
    BreakevenLockParams,
    BreakevenLockStop,
    BreakevenSafeStop,
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
    # The third argument is the PROFIT TRIGGER (level_profit), one noise margin
    # above the margin line — the boundary above which the profit zone governs.
    # Here: break-even=100, margin=110 → profit trigger = 2×110 − 100 = 120.

    def test_at_or_below_break_even_is_underwater(self):
        assert classify_zone(99.0, 100.0, 120.0) is StopZone.UNDERWATER
        assert classify_zone(100.0, 100.0, 120.0) is StopZone.UNDERWATER

    def test_between_break_even_and_profit_trigger_is_band(self):
        assert classify_zone(105.0, 100.0, 120.0) is StopZone.BREAKEVEN_BAND
        # Above the MARGIN line (110) but below the profit trigger (120) is still
        # the break-even band — this is what stops a bid hovering just above the
        # margin from skipping the (thin) band into the profit zone.
        assert classify_zone(115.0, 100.0, 120.0) is StopZone.BREAKEVEN_BAND
        assert classify_zone(120.0, 100.0, 120.0) is StopZone.BREAKEVEN_BAND

    def test_above_profit_trigger_is_profit(self):
        assert classify_zone(120.1, 100.0, 120.0) is StopZone.PROFIT


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
    ):
        return StopContext(
            current_bid=current_bid,
            level_open=level_zero,
            level_zero=level_zero,
            level_margin=level_margin,
            level_follower=level_follower,
            atr_value=atr(list(buf.candles), 14),
            spread=buf.last.spread,
            euro_per_point=euro_per_point,
            buf=buf,
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
        assert ctx.level_zero < new_stop < ctx.current_bid

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
        assert ctx.level_zero < new_stop < ctx.current_bid

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


class TestBreakevenHalf:
    # ``breakeven_half`` raises the stop ONCE, to a support line a quarter of the
    # way from break-even up to the margin level, after two consecutive rising
    # ticks whose closes both clear the margin line. It then holds that stop.

    def test_locks_the_quarter_support_after_two_ticks_above_margin(self):
        # level_zero=8000, level_margin=8010 → support at 8000 + 0.25×10 = 8002.5.
        # The tail 8011 < 8012 < 8013 is two rising ticks above the 8010 margin;
        # the bid has since pulled back into the band at 8005 (> support).
        buf = _buffer([8005.0, 8011.0, 8012.0, 8013.0])
        ctx = _ctx(
            buf,
            current_bid=8005.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        new_stop = BreakevenHalfStop().propose(ctx)
        assert new_stop == pytest.approx(8002.5)
        assert ctx.level_zero < new_stop < ctx.current_bid

    def test_holds_without_two_ticks_above_the_margin(self):
        # The rise stays inside the band (8009 never clears the 8010 margin), so the
        # above-margin streak gate never opens.
        buf = _buffer([8005.0, 8006.0, 8007.0, 8008.0, 8009.0])
        ctx = _ctx(
            buf,
            current_bid=8009.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        assert BreakevenHalfStop().propose(ctx) is None

    def test_holds_when_only_one_tick_clears_the_margin(self):
        # A single close above the margin (8011) is not two consecutive up-ticks
        # above it — the gate needs the whole streak above the line.
        buf = _buffer([8005.0, 8008.0, 8011.0, 8008.0])
        ctx = _ctx(
            buf,
            current_bid=8008.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        assert BreakevenHalfStop().propose(ctx) is None

    def test_holds_when_the_support_would_reach_the_bid(self):
        # The streak above the margin has fired, but the bid has pulled all the way
        # back to 8002 (< the 8002.5 support), so locking there would force an
        # immediate exit → hold.
        buf = _buffer([8005.0, 8011.0, 8012.0, 8013.0])
        ctx = _ctx(
            buf,
            current_bid=8002.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        assert BreakevenHalfStop().propose(ctx) is None

    def test_raises_only_once_while_in_the_margin_zone(self):
        # Follower already above break-even → the single raise has been done (or the
        # profit zone moved it); hold for the rest of the zone, never raise again.
        buf = _buffer([8005.0, 8011.0, 8012.0, 8013.0])
        ctx = _ctx(
            buf,
            current_bid=8005.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=8002.5,  # already above break-even
        )
        assert BreakevenHalfStop().propose(ctx) is None


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
