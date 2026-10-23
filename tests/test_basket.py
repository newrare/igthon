"""Tests for the ``basket`` whole-book close (src/exit/zones/basket.py).

The decision is the pure :func:`explain_basket`: close every position at 17:30,
close every position once the book clears its +1 ATR15 target, flag the 11:00
review otherwise. The composer wiring (group member, ``group_close``) is covered
in ``tests/test_close_zoneprofit.py``.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from src.exit.zones import (
    BASKET_EOD_REASON,
    BASKET_WIN_REASON,
    ZONEPROFIT_UPDATERS,
    ZONESTART_UPDATERS,
    BasketCloseStop,
    BasketParams,
    GroupMember,
    basket_target_euro,
    explain_basket,
)

PARIS = ZoneInfo("Europe/Paris")
# Haircut off so the expected euros are the raw (price − open) × €/point.
_P = BasketParams(reconcile_margin_pct=0.0)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 22, hour, minute, tzinfo=PARIS)


def _leg(
    position_id: int,
    *,
    price: float,
    level_open: float = 100.0,
    sign: float = 1.0,
    euro_per_point: float = 1.0,
    euro_risk: float = 80.0,
) -> GroupMember:
    return GroupMember(
        position_id=position_id,
        level_open=level_open,
        level_follower=level_open - sign * 40.0,
        euro_per_point=euro_per_point,
        current_price=price,
        min_stop_distance=0.0,
        noise=0.0,
        sign=sign,
        euro_risk=euro_risk,
    )


class TestRegistry:
    def test_registered_in_zone_one_and_profit_zone(self):
        assert ZONESTART_UPDATERS["basket"] is BasketCloseStop
        assert ZONEPROFIT_UPDATERS["basket"] is BasketCloseStop

    def test_never_moves_a_stop(self):
        assert BasketCloseStop().propose(object()) is None


class TestTarget:
    def test_one_atr15_of_an_average_leg(self):
        # 80 € risk spread over 8 ATR15 → 10 € per ATR15.
        legs = [_leg(1, price=100), _leg(2, price=100, euro_risk=160.0)]
        assert basket_target_euro(legs, _P) == 15.0

    def test_unknown_when_a_leg_has_no_euro_risk(self):
        legs = [_leg(1, price=100), _leg(2, price=100, euro_risk=0.0)]
        assert basket_target_euro(legs, _P) is None


class TestDecision:
    def test_green_book_above_target_closes_everything(self):
        legs = [_leg(1, price=112), _leg(2, price=97), _leg(3, price=100)]

        report = explain_basket(legs, now=_at(10, 20), params=_P)

        assert report.total_euro == 9.0
        assert report.gate_euro == 10.0
        assert report.close_reason is None
        legs[0] = _leg(1, price=114)
        report = explain_basket(legs, now=_at(10, 20), params=_P)
        assert report.armed
        assert report.close_reason == BASKET_WIN_REASON
        assert report.close_only

    def test_a_short_gains_when_price_falls(self):
        legs = [_leg(1, price=88, sign=-1.0)]

        report = explain_basket(legs, now=_at(10, 20), params=_P)

        assert report.total_euro == 12.0
        assert report.close_reason == BASKET_WIN_REASON

    def test_banked_legs_count_in_the_book(self):
        # Two open legs at +12 €, but a third was stopped out today at −20 €.
        legs = [_leg(1, price=106), _leg(2, price=106)]

        report = explain_basket(legs, now=_at(10, 20), banked_euro=-20.0, params=_P)

        assert report.total_euro == -8.0
        assert report.close_reason is None

    def test_no_target_no_win_close(self):
        legs = [_leg(1, price=150, euro_risk=0.0)]

        report = explain_basket(legs, now=_at(10, 20), params=_P)

        assert report.close_reason is None

    def test_unpriceable_leg_blocks_the_win_close(self):
        legs = [_leg(1, price=150), _leg(2, price=100, euro_per_point=0.0)]

        report = explain_basket(legs, now=_at(10, 20), params=_P)

        assert report.unpriceable == [2]
        assert report.close_reason is None

    def test_haircut_is_pessimistic(self):
        legs = [_leg(1, price=110), _leg(2, price=95)]

        report = explain_basket(legs, now=_at(10, 20))

        assert report.total_euro == 10.0 * 0.98 - 5.0 * 1.02


class TestClock:
    def test_review_is_due_at_eleven_when_still_red(self):
        legs = [_leg(1, price=95)]

        assert not explain_basket(legs, now=_at(10, 59), params=_P).review_due
        assert explain_basket(legs, now=_at(11, 0), params=_P).review_due

    def test_no_review_when_the_book_closes(self):
        legs = [_leg(1, price=120)]

        report = explain_basket(legs, now=_at(11, 30), params=_P)

        assert report.close_reason == BASKET_WIN_REASON
        assert not report.review_due

    def test_half_past_five_closes_whatever_the_book(self):
        legs = [_leg(1, price=60, euro_risk=0.0)]

        report = explain_basket(legs, now=_at(17, 30), params=_P)

        assert report.close_reason == BASKET_EOD_REASON
        assert report.armed

    def test_clock_is_read_in_paris(self):
        # 15:30 UTC is 17:30 in Paris (summer time).
        utc = datetime(2026, 7, 1, 15, 30, tzinfo=ZoneInfo("UTC"))

        report = explain_basket([_leg(1, price=95)], now=utc, params=_P)

        assert report.close_reason == BASKET_EOD_REASON
