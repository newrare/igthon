"""Tests for the per-zone stop updaters and the zone classifier.

The close profile splits per-tick stop management into three zones by where the
live bid sits relative to break-even (``level_zero``) and the margin level
(``level_margin``). Zones 1 and 2 hold the stop; zone 3 runs the momentum-gated
ATR chandelier that trails the bid up in steps.
"""

from datetime import UTC, datetime, timedelta

from src.core.indicators import atr
from src.exit.zones import (
    BreakevenBandStop,
    BreakevenLockStop,
    StopContext,
    StopZone,
    TrailingRatchetStop,
    UnderwaterStop,
    classify_zone,
)
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
    # ``_buffer`` builds a monotonically rising bid, so ``adverse_tick_noise`` is
    # 0 and the required gap floors on ``2 × spread`` (= 1.0 at spread 0.5). The
    # lock target is ``level_zero + 1 × spread`` (= 8000.5 below).

    def test_locks_a_hair_above_break_even_when_bid_is_clear(self):
        buf = _buffer([8000.0 + i for i in range(40)])
        ctx = _ctx(
            buf,
            current_bid=8005.0,  # 4.5 above the 8000.5 target → clears the 1.0 gap
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        new_stop = BreakevenLockStop().propose(ctx)
        assert new_stop == 8000.5
        assert ctx.level_zero < new_stop < ctx.current_bid
        assert new_stop > ctx.level_follower

    def test_holds_while_bid_hugs_break_even(self):
        # Bid only a hair above break-even → gap to the lock target is under the
        # noise floor → do not pull the stop up into the noise.
        buf = _buffer([8000.0 + i for i in range(40)])
        ctx = _ctx(
            buf,
            current_bid=8000.8,  # 0.3 above the 8000.5 target < the 1.0 gap
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=7950.0,
        )
        assert BreakevenLockStop().propose(ctx) is None

    def test_never_lowers_an_already_pushed_stop(self):
        # Follower already above the lock target (e.g. pushed by the profit zone on
        # an earlier excursion) → hold rather than pull it back down.
        buf = _buffer([8000.0 + i for i in range(40)])
        ctx = _ctx(
            buf,
            current_bid=8005.0,
            level_zero=8000.0,
            level_margin=8010.0,
            level_follower=8001.0,  # > 8000.5 target
        )
        assert BreakevenLockStop().propose(ctx) is None


class TestTrailingRatchet:
    def test_rising_bids_far_in_profit_ratchets_up(self):
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

    def test_single_spike_does_not_ratchet(self):
        # A lone up-spike preceded by a down-step → only one rising step → hold.
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
        assert TrailingRatchetStop().propose(ctx) is None

    def test_new_stop_never_lands_in_the_dead_band(self):
        # Rising and in profit, but the trailed stop would fall at/below the margin
        # level → hold rather than park the stop in the band.
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
        assert TrailingRatchetStop().propose(ctx) is None

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
        narrow = TrailingRatchetStop(atr_k_pre=2.5, atr_k_post=2.5).propose(
            _ctx(buf, **kw)
        )
        wide = TrailingRatchetStop(atr_k_pre=3.5, atr_k_post=3.5).propose(
            _ctx(buf, **kw)
        )
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
        without = TrailingRatchetStop(noise_mult=0.0).propose(_ctx(buf, **kw))
        with_floor = TrailingRatchetStop(noise_mult=5.0).propose(_ctx(buf, **kw))
        assert without is not None and with_floor is not None
        assert with_floor < without  # noise floor → stop further below the bid
