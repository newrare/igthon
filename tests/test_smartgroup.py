"""Tests for the ``smartgroup`` zone-1 updater and its portfolio pre-pass.

Unlike the other stop updaters, ``smartgroup`` decides using the whole book: every
open position is valued at "its live price minus its own noise", and if that book
total is a net gain the stops of **all** of them — winners included — are parked
on those levels at once. The heavy lifting is the pure
:func:`plan_group_tightening`, so most coverage lives there;
:class:`SmartGroupStop` is just a reader of the pre-resolved level.
"""

import pytest

from src.exit.zones import (
    GroupMember,
    SmartGroupParams,
    SmartGroupStop,
    StopContext,
    candidate_stop,
    plan_group_tightening,
)
from src.feed.price_buffer import EpicBuffer

_P = SmartGroupParams()


def _member(
    position_id: int,
    *,
    level_open: float,
    level_follower: float,
    price: float,
    euro_per_point: float = 10.0,
    min_stop_distance: float = 0.2,
    noise: float = 0.3,
    sign: float = 1.0,
) -> GroupMember:
    """A group member; ``price`` is the live close-out price (bid / offer)."""
    return GroupMember(
        position_id=position_id,
        level_open=level_open,
        level_follower=level_follower,
        euro_per_point=euro_per_point,
        current_price=price,
        min_stop_distance=min_stop_distance,
        noise=noise,
        sign=sign,
    )


def _winner(position_id: int = 1, *, price: float = 120.0, **kw) -> GroupMember:
    """A long well past break-even: candidate 119.7 → +197 € at 10 €/point."""
    kw.setdefault("level_follower", 110.0)
    return _member(position_id, level_open=100.0, price=price, **kw)


def _flat_loser(position_id: int, **kw) -> GroupMember:
    """A long hovering just under break-even: candidate 49.2 → −8 €."""
    return _member(position_id, level_open=50.0, level_follower=45.0, price=49.5, **kw)


def _sinking_loser(position_id: int, **kw) -> GroupMember:
    """A long drifting down towards its stop: candidate 45.7 → −43 €."""
    return _member(position_id, level_open=50.0, level_follower=45.0, price=46.0, **kw)


class TestCandidateStop:
    """The candidate is the live price stepped back one cushion, adverse side."""

    def test_long_candidate_sits_below_the_bid(self):
        assert candidate_stop(_flat_loser(1)) == pytest.approx(49.2)

    def test_short_candidate_sits_above_the_offer(self):
        m = _member(1, level_open=100.0, level_follower=105.0, price=95.0, sign=-1.0)
        assert candidate_stop(m) == pytest.approx(95.3)

    def test_cushion_is_floored_at_the_min_stop_distance(self):
        m = _flat_loser(1, noise=0.05, min_stop_distance=0.6)
        assert candidate_stop(m) == pytest.approx(48.9)


class TestArmingGate:
    """Nothing moves unless the book is already green at the candidate levels."""

    def test_book_negative_holds_everything(self):
        # Small winner (+47 €) cannot carry the four losers (−102 €).
        members = [
            _winner(price=105.0),
            _flat_loser(2),
            _flat_loser(3),
            _sinking_loser(4),
            _sinking_loser(5),
        ]
        assert plan_group_tightening(members, _P) == {}

    def test_book_positive_tightens_the_whole_book(self):
        # The user's example: 1 big winner (+197 €), 2 flat losers (−8 € each),
        # 2 sinking losers (−43 € each) -> +95 € total, so all five are armed.
        members = [
            _winner(),
            _flat_loser(2),
            _flat_loser(3),
            _sinking_loser(4),
            _sinking_loser(5),
        ]
        plan = plan_group_tightening(members, _P)
        assert set(plan) == {1, 2, 3, 4, 5}
        assert plan[1] == pytest.approx(119.7)  # the winner is raised too
        assert plan[2] == plan[3] == pytest.approx(49.2)
        assert plan[4] == plan[5] == pytest.approx(45.7)

    def test_exactly_break_even_book_does_not_arm(self):
        # Whole-point levels so the total is exactly 0 €: +9 € against −9 €. The
        # gate is strict, so a book that merely breaks even keeps its stops.
        even = dict(euro_per_point=1.0, noise=1.0, min_stop_distance=0.0)
        winner = _member(1, level_open=100.0, level_follower=105.0, price=110.0, **even)
        loser = _member(2, level_open=50.0, level_follower=40.0, price=42.0, **even)
        assert plan_group_tightening([winner, loser], _P) == {}
        # One euro more on the winner's side and the same book arms.
        richer = _member(1, level_open=100.0, level_follower=105.0, price=111.0, **even)
        assert set(plan_group_tightening([richer, loser], _P)) == {1, 2}

    def test_min_group_euro_demands_a_margin(self):
        members = [_winner(), _flat_loser(2)]  # +189 € total
        assert set(plan_group_tightening(members, _P)) == {1, 2}
        strict = SmartGroupParams(min_group_euro=200.0)
        assert plan_group_tightening(members, strict) == {}

    def test_lone_winner_tightens_itself(self):
        # No loser to protect, but the rule is the same: the book is green, so the
        # stop moves onto the noise band.
        assert plan_group_tightening([_winner()], _P) == {1: pytest.approx(119.7)}


class TestSides:
    """Longs and shorts are valued and tightened in the same pass."""

    def test_short_winner_carries_a_long_loser(self):
        short = _member(
            1, level_open=100.0, level_follower=95.0, price=80.0, sign=-1.0
        )  # candidate 80.3 -> +197 €
        plan = plan_group_tightening([short, _flat_loser(2)], _P)
        assert plan[1] == pytest.approx(80.3)
        assert plan[2] == pytest.approx(49.2)

    def test_short_stop_comes_down_only(self):
        # A short's follower already tighter (lower) than the candidate: skipped.
        short = _member(1, level_open=100.0, level_follower=80.1, price=80.0, sign=-1.0)
        plan = plan_group_tightening([short, _flat_loser(2)], _P)
        assert 1 not in plan and 2 in plan


class TestLegality:
    """Illegal candidates are dropped from the plan and valued at the kept stop."""

    def test_candidate_not_beating_the_follower_is_dropped(self):
        # The winner's stop already sits above its candidate (119.9 > 119.7): it
        # keeps it, yet its gain still arms the loser's tightening.
        winner = _winner(level_follower=119.9)
        plan = plan_group_tightening([winner, _flat_loser(2)], _P)
        assert 1 not in plan
        assert plan[2] == pytest.approx(49.2)

    def test_zero_cushion_is_dropped(self):
        # No noise and no broker minimum -> the stop would sit on the price and
        # fire on the spot. It is skipped, not clamped.
        flat = _flat_loser(2, noise=0.0, min_stop_distance=0.0)
        plan = plan_group_tightening([_winner(), flat], _P)
        assert 1 in plan and 2 not in plan

    def test_every_planned_stop_sits_behind_its_own_price(self):
        members = [_winner(), _flat_loser(2), _sinking_loser(3)]
        plan = plan_group_tightening(members, _P)
        for m in members:
            assert plan[m.position_id] < m.current_price

    def test_unpriceable_members_are_ignored_entirely(self):
        # Without a size (or a fill level) a member cannot be valued in euros, so
        # it neither arms the plan nor rides it.
        no_size = _winner(2, euro_per_point=0.0)
        no_open = _member(3, level_open=0.0, level_follower=45.0, price=49.5)
        assert plan_group_tightening([no_size, no_open], _P) == {}
        plan = plan_group_tightening([_winner(), no_size, no_open], _P)
        assert set(plan) == {1}


class TestValuedAtTheStopItKeeps:
    """A member that will not be moved is valued at its stop, not its candidate.

    Counting a skipped member at the candidate it never reaches is the one way the
    "the book is already green" claim can be false: the euros are then paper, not
    protected by anything resting in the market.
    """

    def test_untightenable_winner_counts_its_stop_not_its_paper_profit(self):
        # Plateau winner: noise 0 and no broker minimum, so it cannot be tightened
        # at all. Its paper profit is +200 € (price 120) but its stop only
        # guarantees +100 € (follower 110) — not enough to carry a −153 € loser.
        plateau = _member(
            1,
            level_open=100.0,
            level_follower=110.0,
            price=120.0,
            noise=0.0,
            min_stop_distance=0.0,
        )
        loser = _member(2, level_open=50.0, level_follower=30.0, price=35.0)
        # Valued at the candidate the book would read +47 € and arm; valued at the
        # stop it keeps it reads −53 €, which is the truth, so nothing moves.
        assert plan_group_tightening([plateau, loser], _P) == {}

    def test_a_kept_better_stop_counts_at_its_full_value(self):
        # The mirror case: the winner's follower (119.9) beats its candidate
        # (119.7), so it guarantees +199 € rather than +197 €. That extra 2 € is
        # what carries the −198.5 € loser over the line.
        winner = _winner(level_follower=119.9)
        loser = _member(2, level_open=50.0, level_follower=25.0, price=30.45)
        plan = plan_group_tightening([winner, loser], _P)
        assert plan == {2: pytest.approx(30.15)}

    def test_an_unmovable_unprotected_member_disarms_everything(self):
        # No stop at all and no cushion to place one: unbounded downside, so the
        # book claim cannot be made even though the winner alone is green.
        naked = _member(
            2,
            level_open=50.0,
            level_follower=0.0,
            price=49.5,
            noise=0.0,
            min_stop_distance=0.0,
        )
        assert plan_group_tightening([_winner(), naked], _P) == {}


class TestSmartGroupStop:
    """The updater is a pure reader of the pre-resolved group decision."""

    def _ctx(self, group_tighten):
        return StopContext(
            current_price=49.0,
            level_open=50.0,
            level_zero=50.5,
            level_margin=51.0,
            level_follower=45.0,
            atr_value=1.0,
            spread=0.5,
            euro_per_point=10.0,
            buf=EpicBuffer(epic="TEST.EPIC", max_candles=10),
            group_tighten=group_tighten,
        )

    def test_propose_returns_group_level(self):
        assert SmartGroupStop().propose(self._ctx(48.5)) == 48.5

    def test_propose_holds_without_a_group_level(self):
        assert SmartGroupStop().propose(self._ctx(None)) is None

    def test_registered_and_named(self):
        from src.exit.zones import ZONESTART_UPDATERS

        assert ZONESTART_UPDATERS["smartgroup"] is SmartGroupStop
        assert SmartGroupStop.name == "smartgroup"

    def test_plan_delegates_to_pure_function(self):
        members = [_winner(), _flat_loser(2)]
        assert SmartGroupStop().plan(members) == plan_group_tightening(members, _P)
