"""Tests for the ``adaptive`` zone updater (``src/exit/zones/adaptive.py``).

The updater reads the curve **since the open** and decides hold-or-close, with the
answer depending on the zone the price sits in. Two things are therefore tested
separately: the reading (``read_situation`` — is the market trending, tiring, flat
or turned?) and the decision it drives per zone (including the affordability
arithmetic that prices a flat exit against the day's banked result).

Every case is also run mirrored for a SELL where the direction can change the
answer: the whole profile is written in profit terms, so a short must read exactly
like a long turned upside down.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.core.indicators import atr
from src.exit.base import ACTION_CLOSE, ACTION_HOLD
from src.exit.zones import ZONEMARGE_UPDATERS, ZONESECURE_UPDATERS, ZONESTART_UPDATERS
from src.exit.zones.adaptive import (
    REASON_FLAT_BREAKEVEN,
    REASON_FLAT_CUT,
    REASON_FLAT_PROFIT,
    REASON_REVERSAL,
    AdaptiveReadStop,
    Situation,
)
from src.exit.zones.base import DayBook, StopContext
from src.feed.price_buffer import Candle, EpicBuffer

_START = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)
_SPREAD = 0.5


def _buffer(closes: list[float], spread: float = _SPREAD) -> EpicBuffer:
    """A buffer whose bid closes follow ``closes``, one candle per minute."""
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
    closes: list[float],
    *,
    level_zero: float,
    level_margin: float,
    direction: str = "BUY",
    level_follower: float = 0.0,
    euro_per_point: float = 1.0,
    day_book: DayBook | None = None,
    current_price: float | None = None,
    spread: float = _SPREAD,
) -> StopContext:
    """A context whose whole buffer is the position's life (opened at ``_START``).

    ``closes`` are bid closes; the close-out price is the bid for a BUY and the
    offer for a SELL, mirroring what the close profile builds live.
    """
    buf = _buffer(closes, spread)
    price = current_price
    if price is None:
        price = closes[-1] + (spread if direction == "SELL" else 0.0)
    return StopContext(
        current_price=price,
        level_open=level_zero,
        level_zero=level_zero,
        level_margin=level_margin,
        level_follower=level_follower,
        atr_value=atr(list(buf.candles), 14),
        spread=spread,
        euro_per_point=euro_per_point,
        buf=buf,
        full_buf=buf,
        direction=direction,
        opened_at=_START,
        day_book=day_book if day_book is not None else DayBook(),
    )


def _ctx_with_history(
    history: list[float],
    closes: list[float],
    *,
    level_zero: float,
    level_margin: float,
    direction: str = "BUY",
    day_book: DayBook | None = None,
) -> StopContext:
    """Like :func:`_ctx`, but with ``history`` candles recorded **before** the open.

    The default context is a buffer that starts at the open, which is all the
    since-open reading needs. The background-trend guard reads the *market* curve
    instead (:attr:`~src.core.reading.PositionReading.market`), so it only says
    anything on a buffer holding more than the position's own life.
    """
    buf = _buffer(history + closes)
    opened_at = _START + timedelta(minutes=len(history))
    return StopContext(
        current_price=closes[-1] + (_SPREAD if direction == "SELL" else 0.0),
        level_open=level_zero,
        level_zero=level_zero,
        level_margin=level_margin,
        level_follower=0.0,
        atr_value=atr(list(buf.candles), 14),
        spread=_SPREAD,
        euro_per_point=1.0,
        buf=buf,
        full_buf=buf,
        direction=direction,
        opened_at=opened_at,
        day_book=day_book if day_book is not None else DayBook(),
    )


def _mirror(closes: list[float], pivot: float = 200.0) -> list[float]:
    """The same curve upside down — a SELL's market for a BUY's ``closes``."""
    return [2 * pivot - close for close in closes]


def _sides(direction: str, level_zero: float, level_margin: float) -> dict:
    """The BUY levels, mirrored for a SELL so both sides read the same curve."""
    if direction == "BUY":
        return {"level_zero": level_zero, "level_margin": level_margin}
    return {
        "level_zero": _mirror([level_zero])[0],
        "level_margin": _mirror([level_margin])[0],
    }


# A steady climb: the trade is doing what it was opened for.
_TREND_UP = [100.0 + i * 0.6 for i in range(25)]
# A climb that stalls dead flat for the last quarter of an hour.
_STALLED = [100.0 + i * 0.6 for i in range(10)] + [105.4, 105.5] * 8
# A climb, then a clean fall well past a candle's range.
_TURNED = [100.0 + i * 0.6 for i in range(12)] + [106.6 - i * 0.8 for i in range(1, 14)]
# An hour and a half of steady climb *before* the position was opened — the
# background the zone-1 guard reads.
_RISING_BACKGROUND = [60.0 + i * 0.5 for i in range(80)]
# The same length of background going the other way.
_FALLING_BACKGROUND = [140.0 - i * 0.5 for i in range(80)]
# A climb that keeps going but at a fraction of its earlier speed.
_TIRING = [100.0 + i * 1.2 for i in range(12)] + [
    114.2 + i * 0.05 for i in range(1, 14)
]
# The real curve of live position 929 (EUR/NZD BUY, 2026-09-02), in pips from the
# entry and scaled into this module's price units: eleven candles that end 6.6
# higher — 2.8 candle-ranges of progress — by way of a full round trip back to the
# open in the middle. Its regression fits terribly (R² 0.22) and it clears the band
# it wanders in 3.6 times over, which is why the reading is taken on the band: the
# R² test that shipped first banked this position at +56.94 € eleven minutes in,
# calling a jagged climb a fade.
_JAGGED_CLIMB = [
    round(100.0 + pip * 0.6, 3)
    for pip in [0, 2.2, 7.2, 7.9, 6.3, 4.2, 6.5, 3.5, 0.4, 9.7, 11.0]
]
# The same zigzag whose last leg stops short: it still ends higher than it started,
# but by less than twice the thickness of its own band — progress that is no bigger
# than the noise carrying it.
_INSIDE_THE_BAND = [
    round(100.0 + pip * 0.6, 3)
    for pip in [0, 2.2, 7.2, 7.9, 6.3, 4.2, 6.5, 3.5, 0.4, 3.8, 4.8]
]


class TestRegistration:
    """``adaptive`` is selectable in the three zones it manages, and only those."""

    def test_registered_in_the_first_three_zones(self):
        assert ZONESTART_UPDATERS["adaptive"] is AdaptiveReadStop
        assert ZONEMARGE_UPDATERS["adaptive"] is AdaptiveReadStop
        assert ZONESECURE_UPDATERS["adaptive"] is AdaptiveReadStop

    def test_never_moves_a_stop(self):
        """It is a hold-or-close layer: the stop is left to the other zones."""
        ctx = _ctx(_TURNED, level_zero=100.0, level_margin=102.0)
        assert AdaptiveReadStop().propose(ctx) is None


class TestReadSituation:
    """The reading itself — what the curve since the open is doing."""

    @pytest.mark.parametrize("direction", ["BUY", "SELL"])
    def test_steady_progress_reads_as_a_trend(self, direction):
        closes = _TREND_UP if direction == "BUY" else _mirror(_TREND_UP)
        ctx = _ctx(closes, direction=direction, **_sides(direction, 100.0, 102.0))
        assert AdaptiveReadStop().read_situation(ctx.reading()) is Situation.TREND

    @pytest.mark.parametrize("direction", ["BUY", "SELL"])
    def test_a_market_going_nowhere_reads_as_flat(self, direction):
        closes = _STALLED if direction == "BUY" else _mirror(_STALLED)
        ctx = _ctx(closes, direction=direction, **_sides(direction, 100.0, 102.0))
        assert AdaptiveReadStop().read_situation(ctx.reading()) is Situation.FLAT

    @pytest.mark.parametrize("direction", ["BUY", "SELL"])
    def test_a_move_that_worked_then_broke_down_reads_as_a_reversal(self, direction):
        """Break-even at 100: the curve peaks at 106.6 before falling to 96.2."""
        closes = _TURNED if direction == "BUY" else _mirror(_TURNED)
        ctx = _ctx(closes, direction=direction, **_sides(direction, 100.0, 102.0))
        assert AdaptiveReadStop().read_situation(ctx.reading()) is Situation.REVERSAL

    def test_a_trade_that_never_worked_is_not_a_reversal(self):
        """The rule that cost 718 € on 2026-08-28: no prior gain, no reversal.

        The same falling curve, entered at its top instead of before its rally, has
        never once been in profit. That is not a trend turning — it is the ordinary
        drawdown the protective stop is sized for, and cutting it early trades a
        small certain loss against a recovery it can no longer catch.
        """
        ctx = _ctx(_TURNED, level_zero=112.0, level_margin=114.0)
        reading = ctx.reading()
        assert reading.mfe == 0.0
        assert AdaptiveReadStop().read_situation(reading) is not Situation.REVERSAL

    def test_a_reversal_needs_a_position_older_than_its_window(self):
        """A window as long as the whole trade cannot show a before and an after."""
        # Same shape, compressed into 18 candles: it peaks, it breaks down, but the
        # position is younger than ``reversal_min_minutes``.
        closes = [100.0 + i * 1.2 for i in range(8)] + [
            108.4 - i * 1.4 for i in range(1, 11)
        ]
        ctx = _ctx(closes, level_zero=100.0, level_margin=102.0)
        assert (
            AdaptiveReadStop().read_situation(ctx.reading()) is not Situation.REVERSAL
        )

    def test_a_one_candle_top_is_not_an_established_turn(self):
        """The peak must be a few candles behind, not the print we just made."""
        closes = [100.0 + i * 0.6 for i in range(24)] + [113.8, 106.0]
        ctx = _ctx(closes, level_zero=100.0, level_margin=102.0)
        reading = ctx.reading()
        assert reading.minutes_since_peak < 5
        assert AdaptiveReadStop().read_situation(reading) is not Situation.REVERSAL

    @pytest.mark.parametrize("direction", ["BUY", "SELL"])
    def test_a_slowing_climb_reads_as_fading(self, direction):
        closes = _TIRING if direction == "BUY" else _mirror(_TIRING)
        ctx = _ctx(closes, direction=direction, **_sides(direction, 100.0, 102.0))
        assert AdaptiveReadStop().read_situation(ctx.reading()) is Situation.FADING

    @pytest.mark.parametrize("direction", ["BUY", "SELL"])
    def test_a_jagged_climb_is_still_a_trend(self, direction):
        """Position 929: the move is real, only its path is noisy.

        The regression fit is worthless on this curve, and that is exactly the
        point — the progression clears the band the price wanders in several times
        over, so the zigzag inside it is the epic's own noise and not a move
        running out. Judged on the fit it read ``FADING`` and the position was
        banked eleven minutes in.
        """
        closes = _JAGGED_CLIMB if direction == "BUY" else _mirror(_JAGGED_CLIMB)
        ctx = _ctx(closes, direction=direction, **_sides(direction, 100.0, 102.0))
        reading = ctx.reading()
        updater = AdaptiveReadStop()
        window = min(updater.window, reading.minutes_open)
        # The fit is a wander and the move is worth 3.6 bands: the two measures
        # disagree, and the band is the one the reading is taken on.
        assert reading.curve.trend(window).r_squared < updater.clean_r2
        progress = reading.favourable_closes[-1] - reading.favourable_closes[-window]
        band = reading.curve.amplitude(window).band_noise
        assert progress > updater.trend_band_k * band
        assert updater.read_situation(reading) is Situation.TREND

    @pytest.mark.parametrize("direction", ["BUY", "SELL"])
    def test_progress_inside_its_own_band_reads_as_fading(self, direction):
        """The other side of the band test: a net move no bigger than the noise.

        Not flat — the window did end higher than it started, by more than one bad
        candle gives back — but the progression no longer stands out of the band it
        is travelling in, which is the readable version of "this is running out".
        """
        closes = _INSIDE_THE_BAND if direction == "BUY" else _mirror(_INSIDE_THE_BAND)
        ctx = _ctx(closes, direction=direction, **_sides(direction, 100.0, 102.0))
        reading = ctx.reading()
        updater = AdaptiveReadStop()
        window = min(updater.window, reading.minutes_open)
        progress = reading.favourable_closes[-1] - reading.favourable_closes[-window]
        band = reading.curve.amplitude(window).band_noise
        assert 0 < progress < updater.trend_band_k * band
        assert updater.read_situation(reading) is Situation.FADING

    def test_a_young_position_is_unreadable(self):
        """Nothing is decided on a curve too short to say anything honest about."""
        ctx = _ctx(_STALLED[:6], level_zero=100.0, level_margin=102.0)
        assert AdaptiveReadStop().read_situation(ctx.reading()) is Situation.UNREADABLE

    def test_an_ordinary_pullback_is_not_a_reversal(self):
        """An adverse move under one ATR is the noise the stop is sized for."""
        closes = [100.0 + i * 0.6 for i in range(20)] + [111.2, 111.0, 110.8, 110.6]
        ctx = _ctx(closes, level_zero=100.0, level_margin=102.0)
        situation = AdaptiveReadStop().read_situation(ctx.reading())
        assert situation is not Situation.REVERSAL


class TestUnderwaterZone:
    """Zone 1 — closing costs money, so the day's result decides the flat case."""

    def test_reversal_closes_immediately(self):
        """It worked, then it turned and went under: no reason to pay the rest of R.

        Break-even at 101, so the curve's rally to 106.6 was a real gain before the
        fall carried it to 96.2 — under water, with the turn behind it.
        """
        ctx = _ctx(_TURNED, level_zero=101.0, level_margin=103.0)
        assert AdaptiveReadStop().decide(ctx).close_reason == REASON_REVERSAL

    def test_a_losing_trade_that_never_worked_is_held(self):
        """Entered at the top of the same curve: held, the stop governs it."""
        ctx = _ctx(_TURNED, level_zero=112.0, level_margin=114.0)
        assert AdaptiveReadStop().decide(ctx).close_reason is None

    def test_flat_closes_when_the_day_can_afford_it(self):
        """-4.5 € against a 20 € budget (20 % of the 100 € the day banked)."""
        ctx = _ctx(
            _STALLED,
            level_zero=110.0,  # price stalls at 105.5 → -4.5 points → -4.5 €
            level_margin=112.0,
            euro_per_point=1.0,
            day_book=DayBook(realized_euro=100.0),
        )
        assert AdaptiveReadStop().decide(ctx).close_reason == REASON_FLAT_CUT

    def test_flat_holds_when_the_loss_would_eat_the_day(self):
        """Same stagnating position, a day holding only 5 € (1 € budget): kept."""
        ctx = _ctx(
            _STALLED,
            level_zero=110.0,
            level_margin=112.0,
            euro_per_point=1.0,
            day_book=DayBook(realized_euro=5.0),
        )
        assert AdaptiveReadStop().decide(ctx).close_reason is None

    def test_flat_holds_when_nothing_is_banked(self):
        """A day with no result closes nothing on affordability grounds."""
        ctx = _ctx(_STALLED, level_zero=110.0, level_margin=112.0)
        assert AdaptiveReadStop().decide(ctx).close_reason is None

    def test_flat_holds_when_the_exit_cannot_be_priced(self):
        """A missing €/point must read as "unknown", never as "free"."""
        ctx = _ctx(
            _STALLED,
            level_zero=110.0,
            level_margin=112.0,
            euro_per_point=0.0,
            day_book=DayBook(realized_euro=1000.0),
        )
        assert AdaptiveReadStop().decide(ctx).close_reason is None

    def test_the_budget_is_cumulative_across_the_day(self):
        """Cuts already made today are subtracted — the cap is on their total.

        The first version compared each cut against the day's *net*, which each cut
        lowered, so the allowance regrew after every cut and the series ate the
        whole banked gain (five cuts, −51.52 €, live on 2026-08-28).
        """
        stalled = dict(level_zero=110.0, level_margin=112.0, euro_per_point=1.0)
        # 20 % of 100 € = a 20 € budget for the whole day; 4.5 € fits...
        assert (
            AdaptiveReadStop()
            .decide(_ctx(_STALLED, day_book=DayBook(realized_euro=100.0), **stalled))
            .close_reason
            == REASON_FLAT_CUT
        )
        # ...but not once 17 € of it has already been spent, even though the day's
        # banked total is untouched by those earlier cuts.
        assert (
            AdaptiveReadStop()
            .decide(
                _ctx(
                    _STALLED,
                    day_book=DayBook(realized_euro=100.0, flat_cut_euro=17.0),
                    **stalled,
                )
            )
            .close_reason
            is None
        )

    def test_flat_is_not_cut_before_the_longer_minimum(self):
        """A cut books a loss, so it waits out the upper end of the 10/20 window.

        Three of the five cuts of 2026-08-28 fired at 12-15 minutes of age, one of
        them on a position that had been +10.50 € eight minutes earlier.
        """
        # 18 candles of dead flat: readable, affordable, but too young to cut.
        young = [105.4, 105.5] * 9
        ctx = _ctx(
            young,
            level_zero=110.0,
            level_margin=112.0,
            day_book=DayBook(realized_euro=100.0),
        )
        assert AdaptiveReadStop().read_situation(ctx.reading()) is Situation.FLAT
        assert AdaptiveReadStop().decide(ctx).close_reason is None

    def test_a_fading_climb_is_given_the_chance_to_reach_breakeven(self):
        """Still coming our way and short of break-even: hold, do not bank a loss."""
        ctx = _ctx(_TIRING, level_zero=130.0, level_margin=132.0)
        assert AdaptiveReadStop().read_situation(ctx.reading()) is Situation.FADING
        assert AdaptiveReadStop().decide(ctx).close_reason is None

    def test_a_working_trend_is_held(self):
        ctx = _ctx(_TREND_UP, level_zero=120.0, level_margin=122.0)
        assert AdaptiveReadStop().decide(ctx).close_reason is None


class TestReversalNeedsMoreThanASpread:
    """ "It worked first" is measured against the spread as well as the ATR.

    On an epic whose round trip costs most of a candle range, one ATR of favourable
    excursion is barely more than the cost of the trade — a wick, not a gain. Live
    on 2026-09-01 (position 926, USD/JPY quoted 0.010 wide on a 0.012 ATR): cut as
    a reversal on an MFE of 1.5 pip (1.25 ATR, +1.40 € at best) for −30.43 €, on a
    curve that then ran 10 pips past break-even without approaching its stop.
    """

    #: Entered near the top of ``_TURNED``: the rally is worth ~1.1 ATR of MFE,
    #: enough for the ATR floor alone, and the fall after it is a clean reversal.
    _LEVEL_ZERO = 105.6

    def test_a_wick_sized_excursion_is_not_a_reversal(self):
        ctx = _ctx(_TURNED, level_zero=self._LEVEL_ZERO, level_margin=107.6, spread=1.0)
        reading = ctx.reading()
        assert reading.mfe_in_atr > AdaptiveReadStop().reversal_mfe_atr_k
        assert reading.mfe < 2 * reading.market.spread_avg(30)
        assert AdaptiveReadStop().read_situation(reading) is not Situation.REVERSAL
        assert AdaptiveReadStop().decide(ctx).close_reason is None

    def test_the_same_curve_reverses_once_the_spread_is_small(self):
        """The spread is the only thing that changed — and it decides the call."""
        ctx = _ctx(_TURNED, level_zero=self._LEVEL_ZERO, level_margin=107.6, spread=0.5)
        assert AdaptiveReadStop().read_situation(ctx.reading()) is Situation.REVERSAL

    def test_the_floor_is_the_stricter_of_the_two_units(self):
        rule = AdaptiveReadStop()
        reading = _ctx(
            _TURNED, level_zero=self._LEVEL_ZERO, level_margin=107.6, spread=1.0
        ).reading()
        atr_value = reading.market.atr
        assert rule._min_reversal_mfe(reading, atr_value) == pytest.approx(
            max(
                rule.reversal_mfe_atr_k * atr_value,
                rule.reversal_mfe_spread_k * reading.market.spread_avg(30),
            )
        )


class TestBackgroundTrend:
    """Zone 1 books no loss while the hour behind the position still climbs.

    A fifteen-candle window cannot tell "the move turned" from "the move is
    breathing"; the hour behind it can. Live on 2026-09-01 (position 926): cut as a
    reversal on a −1.75 ATR window while ``trend_60m`` read +0.047 % at R² 0.53 and
    price sat mid-channel — the curve then climbed 10 pips past break-even.
    """

    @pytest.mark.parametrize("direction", ["BUY", "SELL"])
    def test_a_reversal_is_held_while_the_background_still_climbs(self, direction):
        history = (
            _RISING_BACKGROUND
            if direction == "BUY"
            else _mirror(_RISING_BACKGROUND, pivot=100.0)
        )
        closes = _TURNED if direction == "BUY" else _mirror(_TURNED, pivot=100.0)
        levels = (
            {"level_zero": 101.0, "level_margin": 103.0}
            if direction == "BUY"
            else {"level_zero": 99.0, "level_margin": 97.0}
        )
        ctx = _ctx_with_history(history, closes, direction=direction, **levels)
        # The reading is unchanged — it is the decision the background holds back.
        assert AdaptiveReadStop().read_situation(ctx.reading()) is Situation.REVERSAL
        assert AdaptiveReadStop().decide(ctx).close_reason is None

    def test_the_same_reversal_closes_under_a_falling_background(self):
        ctx = _ctx_with_history(
            _FALLING_BACKGROUND, _TURNED, level_zero=101.0, level_margin=103.0
        )
        assert AdaptiveReadStop().decide(ctx).close_reason == REASON_REVERSAL

    def test_the_flat_cut_is_not_guarded_by_the_background(self):
        """Only the reversal is — the archive says guarding the cut too loses money.

        Replayed over the ten archived weeks, extending the guard to the flat cut
        turns 54 cuts into 39 and 31 stop-outs into 38, for −1228 €: a stagnating
        position held against a rising background mostly walks to its stop.
        """
        ctx = _ctx_with_history(
            _RISING_BACKGROUND,
            _STALLED,
            level_zero=110.0,
            level_margin=112.0,
            day_book=DayBook(realized_euro=100.0),
        )
        assert AdaptiveReadStop()._market_still_favourable(ctx.reading()) is True
        assert AdaptiveReadStop().read_situation(ctx.reading()) is Situation.FLAT
        assert AdaptiveReadStop().decide(ctx).close_reason == REASON_FLAT_CUT

    def test_a_wandering_background_does_not_protect_anything(self):
        """Only a *clean* line the trade's way holds a close back."""
        choppy = [100.0 + (i % 2) * 0.2 for i in range(80)]
        ctx = _ctx_with_history(choppy, _TURNED, level_zero=101.0, level_margin=103.0)
        assert ctx.reading().market.trend(60).r_squared < AdaptiveReadStop().clean_r2
        assert AdaptiveReadStop().decide(ctx).close_reason == REASON_REVERSAL

    def test_the_guard_never_holds_a_profitable_exit(self):
        """Zones 2 and 3 bank their gain whatever the background is doing."""
        ctx = _ctx_with_history(
            _RISING_BACKGROUND, _TIRING, level_zero=114.0, level_margin=118.0
        )
        assert AdaptiveReadStop().decide(ctx).close_reason == REASON_FLAT_BREAKEVEN


class TestProfitZones:
    """Zones 2 and 3 — the gain is real, so anything but a clean run banks it."""

    def test_breakeven_band_takes_the_no_loss_exit_on_a_fade(self):
        """Price cleared break-even and the climb is tiring: exit without a loss."""
        ctx = _ctx(_TIRING, level_zero=114.0, level_margin=118.0)
        decision = AdaptiveReadStop().decide(ctx)
        assert decision.close_reason == REASON_FLAT_BREAKEVEN

    def test_secure_zone_banks_the_gain_on_a_flat(self):
        """Past the margin line and going nowhere: the gain will not grow.

        Price stalls at 105.5 with break-even at 100 and the margin at 104, so it
        sits between the margin line and the profit trigger (108) — the secure
        zone, not the trailing one.
        """
        ctx = _ctx(_STALLED, level_zero=100.0, level_margin=104.0)
        decision = AdaptiveReadStop().decide(ctx)
        assert decision.close_reason == REASON_FLAT_PROFIT

    def test_secure_zone_holds_a_clean_run(self):
        """A trend that still works is left for the profit zone's trailing."""
        ctx = _ctx(_TREND_UP, level_zero=110.0, level_margin=113.0)
        assert AdaptiveReadStop().decide(ctx).close_reason is None

    def test_secure_zone_holds_a_jagged_run(self):
        """Position 929 in its own zone: price at 106.6, margin 105, trigger 110.

        The regression test for the exit this rule got wrong live — a noisy climb
        past the margin line is held for the trailing stop, not banked as a fade.
        """
        ctx = _ctx(_JAGGED_CLIMB, level_zero=100.0, level_margin=105.0)
        assert AdaptiveReadStop().decide(ctx).close_reason is None

    def test_reversal_in_profit_closes_too(self):
        ctx = _ctx(_TURNED, level_zero=95.0, level_margin=96.0)
        assert AdaptiveReadStop().decide(ctx).close_reason == REASON_REVERSAL

    @pytest.mark.parametrize(
        "direction,closes", [("BUY", _STALLED), ("SELL", _mirror(_STALLED))]
    )
    def test_a_short_banks_its_gain_the_same_way(self, direction, closes):
        """The mirrored curve must reach the same verdict on the other side."""
        level_zero = 100.0 if direction == "BUY" else 300.0
        level_margin = 104.0 if direction == "BUY" else 296.0
        ctx = _ctx(
            closes,
            level_zero=level_zero,
            level_margin=level_margin,
            direction=direction,
        )
        assert AdaptiveReadStop().decide(ctx).close_reason == REASON_FLAT_PROFIT


class TestThroughTheCloseProfile:
    """The composer must turn a zone's close verdict into a real CLOSE decision."""

    @staticmethod
    def _position(closes: list[float], level_zero: float, level_margin: float):
        from types import SimpleNamespace

        return SimpleNamespace(
            id=1,
            epic="TEST.EPIC",
            direction="BUY",
            date=_START.date(),
            time_open=_START.time(),
            level_open=closes[0],
            level_zero=level_zero,
            level_margin=level_margin,
            level_follower=level_zero - 20.0,
            euro_per_point=1.0,
            min_stop_distance=0.0,
        )

    def test_the_profile_closes_on_a_read_reversal(self):
        from src.exit.close_zoneprofit import CloseZoneProfit

        buf = _buffer(_TURNED)
        position = self._position(_TURNED, 101.0, 103.0)
        profile = CloseZoneProfit(underwater=AdaptiveReadStop())
        decision = profile.evaluate(
            position, buf.last.bid_close, buf, is_close_hour=False
        )
        assert decision.action == ACTION_CLOSE
        assert decision.reason == REASON_REVERSAL

    def test_the_day_result_reaches_the_updater(self):
        """``day_realized_euro`` is threaded from the monitor pass to the zone."""
        from src.exit.close_zoneprofit import CloseZoneProfit

        buf = _buffer(_STALLED)
        position = self._position(_STALLED, 110.0, 112.0)
        profile = CloseZoneProfit(underwater=AdaptiveReadStop())

        held = profile.evaluate(position, buf.last.bid_close, buf, is_close_hour=False)
        assert held.action == ACTION_HOLD

        closed = profile.evaluate(
            position,
            buf.last.bid_close,
            buf,
            is_close_hour=False,
            day_book=DayBook(realized_euro=100.0),
        )
        assert closed.action == ACTION_CLOSE
        assert closed.reason == REASON_FLAT_CUT
