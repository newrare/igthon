"""The exit domain on the SHORT side — one direction-aware profile, four zones.

A SELL is managed by the very same :class:`~src.exit.close_zoneprofit.
CloseZoneProfit` as a BUY, with every reference mirrored: the stop sits above
price, the margin below break-even, and each ``CLOSE_ZONE*`` updater tightens
*downwards*. These tests pin that mirroring zone by zone.

Regression: shorts used to bypass the whole zone stack through a separate
profile that only had the profit-zone chandelier — so a short's stop never moved
inside the break-even→margin band, however far price ran (observed live on
EN.D.ICENG.FWS9.IP, 2026-07-27: break-even 1413.00, margin 1410.21, price held
below the 1407.43 profit trigger for half an hour and the stop stayed on its open
level). ``TestMarginZone`` is that scenario in miniature.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.exit import CloseZoneProfit
from src.exit.base import ACTION_CLOSE, ACTION_HOLD, ACTION_UPDATE_STOP
from src.exit.trailing import compute_trailing_stop
from src.exit.zones.base import StopZone
from src.feed.price_buffer import Candle, EpicBuffer

_START = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)
_SPREAD = 0.5


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "stop_strategy": "stop_support",
        "close_zonestart": "hold",
        "close_zonemarge": "hold",
        "close_zonesecure": "breakeven_half",
        "close_zoneprofit": "trailing_ratchet",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _profile(**overrides) -> CloseZoneProfit:
    return CloseZoneProfit.from_settings(_settings(**overrides))


def _buffer(offers: list[float], spread: float = _SPREAD) -> EpicBuffer:
    """Buffer whose OFFER closes follow ``offers`` (the close-out price of a short).

    Bids are one spread below, wicks 0.1 beyond the candle body, so ATR is small
    but strictly positive.
    """
    buf = EpicBuffer(epic="TEST.EPIC", max_candles=len(offers) + 10)
    prev = offers[0]
    for i, offer in enumerate(offers):
        high = max(prev, offer) + 0.1
        low = min(prev, offer) - 0.1
        buf.add(
            Candle(
                timestamp=_START + timedelta(minutes=i),
                bid_open=prev - spread,
                bid_close=offer - spread,
                bid_high=high - spread,
                bid_low=low - spread,
                offer_open=prev,
                offer_close=offer,
                offer_high=high,
                offer_low=low,
            )
        )
        prev = offer
    return buf


def _position(**overrides) -> SimpleNamespace:
    """A SELL entered at 100 (the bid), so break-even in buy-to-close terms is 100."""
    base = {
        "id": 1,
        "direction": "SELL",
        "level_open": 100.0,
        "level_win": 0.0,
        "level_loose": 0.0,
        "level_zero": 100.0,
        "level_margin": 97.0,  # one noise margin BELOW break-even
        "level_follower": 105.0,  # the short's stop sits ABOVE price
        "euro_per_point": 0.0,
        "euro_stop": 0.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _bid_for(offer: float) -> float:
    """The monitor hands the profile the live BID; a short closes on the offer."""
    return offer - _SPREAD


class TestInitialPlan:
    """Open-time references are mirrored for a SELL."""

    def test_stop_above_entry_and_margin_below_break_even(self):
        buf = _buffer([100.0] * 30)
        plan = _profile().initial_plan(entry_level=100.0, direction="SELL", buf=buf)
        assert plan.stop_level > 100.0
        # A short sells at the bid, so break-even in buy-to-close terms is that bid.
        assert plan.level_zero == 100.0
        assert plan.level_margin < plan.level_zero
        assert plan.profile == "close_zoneprofit"

    def test_mirrors_the_long_margin_band(self):
        buf = _buffer([100.0 + (i % 3) for i in range(30)])
        profile = _profile()
        long_plan = profile.initial_plan(entry_level=100.0, direction="BUY", buf=buf)
        short_plan = profile.initial_plan(entry_level=100.0, direction="SELL", buf=buf)
        long_band = long_plan.level_margin - long_plan.level_zero
        short_band = short_plan.level_zero - short_plan.level_margin
        assert long_band == pytest.approx(short_band)
        assert short_band > 0


class TestZoneClassification:
    """The four zones stack downwards: break-even 100, margin 97, trigger 94."""

    def test_offer_above_break_even_is_underwater(self):
        buf = _buffer([101.0] * 20)
        zone = _profile().current_zone(_position(), _bid_for(101.0), buf)
        assert zone is StopZone.UNDERWATER

    def test_offer_between_break_even_and_margin_is_the_band(self):
        buf = _buffer([98.0] * 20)
        zone = _profile().current_zone(_position(), _bid_for(98.0), buf)
        assert zone is StopZone.BREAKEVEN_BAND

    def test_offer_between_margin_and_trigger_is_the_secure_zone(self):
        buf = _buffer([96.0] * 20)
        zone = _profile().current_zone(_position(), _bid_for(96.0), buf)
        assert zone is StopZone.SECURE

    def test_offer_below_the_profit_trigger_is_the_profit_zone(self):
        buf = _buffer([93.0] * 20)
        zone = _profile().current_zone(_position(), _bid_for(93.0), buf)
        assert zone is StopZone.PROFIT

    def test_classified_on_the_offer_not_the_bid(self):
        # Straddling break-even: the bid (99.6) would read as "in profit", but the
        # offer (100.1) is what a short pays to close and it has not cleared 100.
        buf = _buffer([100.1] * 20)
        assert _profile().current_zone(_position(), 99.6, buf) is StopZone.UNDERWATER


class TestBackstop:
    """The software backstop fires on the offer — the buy-to-close cost."""

    def test_closes_when_the_offer_reaches_the_stop(self):
        buf = _buffer([105.0] * 20)
        decision = _profile().evaluate(
            _position(), _bid_for(105.0), buf, is_close_hour=False
        )
        assert decision.action == ACTION_CLOSE
        assert decision.reason == "stop"

    def test_holds_while_the_offer_is_short_of_the_stop(self):
        buf = _buffer([104.0] * 20)
        decision = _profile().evaluate(
            _position(), _bid_for(104.0), buf, is_close_hour=False
        )
        assert decision.action != ACTION_CLOSE

    def test_bid_below_the_stop_does_not_stop_it_out(self):
        # Regression: comparing a short against the BID would read the position as
        # one spread cheaper to close than it is and fire the backstop late.
        buf = _buffer([105.4] * 20)
        decision = _profile().evaluate(_position(), 104.9, buf, is_close_hour=False)
        assert decision.action == ACTION_CLOSE

    def test_fires_during_atr_warmup(self):
        # Regression (#9): fewer than atr_period candles -> atr()==0 must not
        # disable the only software close after a restart.
        buf = _buffer([105.0, 105.0, 105.0])
        decision = _profile().evaluate(
            _position(), _bid_for(105.0), buf, is_close_hour=False
        )
        assert decision.action == ACTION_CLOSE
        assert decision.reason == "stop"

    def test_end_of_day_closes(self):
        buf = _buffer([99.0] * 20)
        decision = _profile().evaluate(
            _position(), _bid_for(99.0), buf, is_close_hour=True
        )
        assert decision.action == ACTION_CLOSE
        assert decision.reason == "end_of_day"


class TestMarginZone:
    """``limitloose`` pulls the stop down behind the market, above the offer."""

    # A drifting tape inside the band (break-even 100 → margin 97) that gives back
    # 1.0 every other candle, so the epic has a measurable adverse-noise band. For a
    # short the adverse direction is UP, so those give-backs are up-moves.
    _BAND = [99.0 - i * 0.05 + (1.0 if i % 2 else 0.0) for i in range(30)]

    def test_pulls_the_stop_down_to_a_double_noise_band_above_the_offer(self):
        buf = _buffer(self._BAND)
        offer = buf.last.offer_close
        decision = _profile(close_zonemarge="limitloose").evaluate(
            _position(), _bid_for(offer), buf, is_close_hour=False
        )
        assert decision.action == ACTION_UPDATE_STOP
        # Above the live offer (a short's stop sits above price) but far tighter
        # than the 105 stop the position opened with.
        assert offer < decision.new_stop_level < 105.0

    def test_hold_leaves_the_stop_alone(self):
        buf = _buffer(self._BAND)
        offer = buf.last.offer_close
        decision = _profile().evaluate(
            _position(), _bid_for(offer), buf, is_close_hour=False
        )
        assert decision.action == ACTION_HOLD


class TestSecureZone:
    """``breakeven_half`` secures the midpoint at once — the live regression.

    Break-even 100, margin 97 → the stop goes to ``100 − 0.5 × 3 = 98.5`` as soon as
    the offer trades past the 97 margin line, with no confirmation streak.

    This is the scenario shorts used to sit through untouched: price below the
    margin but short of the 94 profit trigger was classified as the break-even band
    and nothing moved the stop.
    """

    # 25 flat candles inside the band, then a push past the 97 margin line.
    _PUSH = [99.0] * 25 + [98.0, 97.0, 96.5, 96.0]

    def test_secures_the_midpoint_at_once(self):
        buf = _buffer(self._PUSH)
        decision = _profile().evaluate(
            _position(), _bid_for(96.0), buf, is_close_hour=False
        )
        assert decision.action == ACTION_UPDATE_STOP
        assert decision.new_stop_level == pytest.approx(98.5)
        # Locked below break-even (real profit for a short) and above the live
        # offer, so the position is not closed on the spot.
        assert 96.0 < decision.new_stop_level < 100.0

    def test_never_loosens_an_existing_stop(self):
        buf = _buffer(self._PUSH)
        # Follower already tighter than the midpoint (lower, for a short) -> hold.
        pos = _position(level_follower=98.0)
        decision = _profile().evaluate(pos, _bid_for(96.0), buf, is_close_hour=False)
        assert decision.action == ACTION_HOLD

    def test_mirrors_the_long_lock(self):
        # The same path reflected about the entry gives the mirrored stop.
        short = _profile().evaluate(
            _position(), _bid_for(96.0), _buffer(self._PUSH), is_close_hour=False
        )
        # Reflect the BID series (a long's close-out price) about the entry, so
        # the offers this helper records are one spread above it.
        long_offers = [200.0 - o + _SPREAD for o in self._PUSH]
        long_pos = _position(
            direction="BUY", level_zero=100.0, level_margin=103.0, level_follower=95.0
        )
        long = _profile().evaluate(
            long_pos, 104.0, _buffer(long_offers), is_close_hour=False
        )
        assert long.action == short.action == ACTION_UPDATE_STOP
        assert long.new_stop_level == pytest.approx(200.0 - short.new_stop_level)


class TestProfitZone:
    """``trailing_ratchet`` trails the offer down once past the profit trigger."""

    def test_ratchets_down_with_falling_momentum(self):
        buf = _buffer([100.0] * 25 + [96.0, 94.0, 92.0, 91.0, 90.0])
        decision = _profile().evaluate(
            _position(), _bid_for(90.0), buf, is_close_hour=False
        )
        assert decision.action == ACTION_UPDATE_STOP
        # Tightened from 105 towards price, still above the offer, and clear of the
        # dead band (the anti-band guard keeps it past the margin line).
        assert 90.0 < decision.new_stop_level < 97.0

    def test_floor_establishes_a_stop_when_momentum_fails(self):
        # Flat tape in deep profit: the momentum gate blocks the chandelier, but the
        # support-anchored lock floor still establishes a stop, so a short that
        # jumped straight past the margin is never left on its open level.
        # swing_low = 90, noise = 0 -> 100 − 0.6 × (100 − 90) = 94.
        buf = _buffer([90.0] * 30)
        decision = _profile().evaluate(
            _position(), _bid_for(90.0), buf, is_close_hour=False
        )
        assert decision.action == ACTION_UPDATE_STOP
        assert decision.new_stop_level == pytest.approx(94.0)

    def test_never_loosens_an_existing_stop(self):
        buf = _buffer([100.0] * 25 + [96.0, 94.0, 92.0, 91.0, 90.0])
        pos = _position(level_follower=90.5)  # already tighter than any proposal
        decision = _profile().evaluate(pos, _bid_for(90.0), buf, is_close_hour=False)
        assert decision.action == ACTION_HOLD


class TestUnderwaterZone:
    """``timedlift`` reviews a losing short's stop once per period, downwards."""

    def test_tightens_the_stop_down_onto_the_period_ceiling(self):
        # 12 minutes of trading with the offer capped around 104.5, price now 103.5:
        # one full 10-minute period has closed, so the stop can come down under it.
        buf = _buffer([104.0, 104.5] * 6 + [103.5] * 4)
        pos = _position(level_follower=110.0)
        decision = _profile(close_zonestart="timedlift").evaluate(
            pos, _bid_for(103.5), buf, is_close_hour=False
        )
        assert decision.action == ACTION_UPDATE_STOP
        # Below the old stop, above the period's worst print, and still short of
        # break-even: this zone reduces risk, it never locks a profit.
        assert 104.6 < decision.new_stop_level < 110.0
        assert decision.new_stop_level > 100.0

    def test_hold_updater_leaves_the_stop_alone(self):
        buf = _buffer([104.0, 104.5] * 6 + [103.5] * 4)
        pos = _position(level_follower=110.0)
        decision = _profile().evaluate(pos, _bid_for(103.5), buf, is_close_hour=False)
        assert decision.action == ACTION_HOLD


class TestTrailingMaths:
    """The signed chandelier is one function for both sides."""

    class _Cfg:
        atr_k_pre = 2.5
        atr_k_post = 2.5
        trailing_step_ratio = 0.3

    def _stop(self, sign: float, follower: float) -> float | None:
        return compute_trailing_stop(
            100.0,
            atr_value=2.0,
            spread=0.0,
            level_zero=0.0,
            level_follower=follower,
            euro_per_point=0.0,
            euro_stop=0.0,
            config=self._Cfg(),
            sign=sign,
        )

    def test_short_stop_sits_above_price(self):
        stop = self._stop(-1.0, 1e9)
        assert stop == pytest.approx(105.0)  # 100 + 2.5 × 2

    def test_short_only_ratchets_down(self):
        assert self._stop(-1.0, 101.0) is None

    def test_short_is_the_mirror_of_the_long(self):
        long_stop = self._stop(1.0, -1e9)
        short_stop = self._stop(-1.0, 1e9)
        assert (100.0 - long_stop) == pytest.approx(short_stop - 100.0)
