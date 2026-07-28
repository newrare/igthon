"""Tests for the ``smartgroup`` zone-1 updater and its portfolio pre-pass.

Unlike the other stop updaters, ``smartgroup`` decides using the whole book: once
enough guaranteed euro is locked in the winners (stop already above break-even),
it spends a fraction of that pot to tighten the underwater positions' stops toward
the bid — priority-greedy by loss-reduction efficiency, cushion width scaled by
budget richness. The heavy lifting is the pure :func:`plan_group_tightening`, so
most coverage lives there; :class:`SmartGroupStop` is just a reader of the
pre-resolved level.
"""

import pytest

from src.exit.zones import (
    GroupMember,
    SmartGroupParams,
    SmartGroupStop,
    StopContext,
    plan_group_tightening,
)
from src.feed.price_buffer import EpicBuffer

_P = SmartGroupParams()


def _member(
    position_id: int,
    *,
    level_open: float,
    level_follower: float,
    current_bid: float,
    euro_per_point: float = 10.0,
    spread: float = 0.5,
    level_zero: float | None = None,
    atr_value: float = 1.0,
    min_stop_distance: float = 0.2,
    noise: float = 0.3,
) -> GroupMember:
    """A group member; break-even defaults to ``level_open + spread``."""
    return GroupMember(
        position_id=position_id,
        level_open=level_open,
        level_zero=level_open + spread if level_zero is None else level_zero,
        level_follower=level_follower,
        euro_per_point=euro_per_point,
        current_price=current_bid,
        atr_value=atr_value,
        spread=spread,
        min_stop_distance=min_stop_distance,
        noise=noise,
    )


def _winner(position_id: int, guaranteed_points: float, **kw) -> GroupMember:
    """A winner whose follower sits ``guaranteed_points`` above the open."""
    return _member(
        position_id,
        level_open=100.0,
        level_follower=100.0 + guaranteed_points,
        current_bid=100.0 + guaranteed_points + 1.0,
        **kw,
    )


def _loser(
    position_id: int, *, open_at=50.0, follower=45.0, bid=49.0, **kw
) -> GroupMember:
    """An underwater loser (bid at/below break-even) with a wide initial stop."""
    return _member(
        position_id, level_open=open_at, level_follower=follower, current_bid=bid, **kw
    )


class TestDisarm:
    """Without guaranteed capital the pre-pass holds everything (pure ``hold``)."""

    def test_no_winners_returns_empty(self):
        assert plan_group_tightening([_loser(1), _loser(2)], _P) == {}

    def test_no_losers_returns_empty(self):
        assert plan_group_tightening([_winner(1, 5.0)], _P) == {}

    def test_zero_euro_per_point_winner_is_not_guaranteed(self):
        m = [_winner(1, 5.0, euro_per_point=0.0), _loser(2)]
        assert plan_group_tightening(m, _P) == {}


class TestArming:
    """The guaranteed pot (× budget fraction) is the loss budget for the losers."""

    def test_large_pot_tightens_all_losers(self):
        # Winner guarantees (20)*10 = 200 €; budget 160 € covers both losers.
        members = [
            _winner(1, 20.0),
            _loser(2),
            _loser(3, open_at=200.0, follower=190.0, bid=199.0, euro_per_point=5.0),
        ]
        plan = plan_group_tightening(members, _P)
        assert set(plan) == {2, 3}
        # Each tightened stop sits below its bid (never an immediate close)...
        assert plan[2] < 49.0 and plan[3] < 199.0
        # ...and above the wide initial follower (a genuine up-only tighten).
        assert plan[2] > 45.0 and plan[3] > 190.0

    def test_small_pot_tightens_only_what_fits(self):
        # Winner guarantees 10 €; budget 8 € — only the cheaper loser fits.
        members = [
            _winner(1, 1.0),
            _loser(2),
            _loser(3, open_at=200.0, follower=190.0, bid=199.0, euro_per_point=5.0),
        ]
        plan = plan_group_tightening(members, _P)
        assert len(plan) == 1

    def test_budget_fraction_leaves_a_safety_cushion(self):
        # A pot that would cover the loss at 100 % but not at 80 % tightens nothing.
        # One loser whose floor-cushion cost is just under the full pot.
        loser = _loser(
            2, open_at=50.0, follower=49.0, bid=49.4, noise=0.1, min_stop_distance=0.1
        )
        # floor cushion 0.1 -> stop 49.3 -> cost (50-49.3)*10 = 7 €.
        full_pot = _winner(1, 0.8)  # guaranteed 8 €; 80 % -> budget 6.4 € < 7 €.
        assert plan_group_tightening([full_pot, loser], _P) == {}
        richer = _winner(1, 0.9)  # guaranteed 9 €; 80 % -> 7.2 € >= 7 €.
        assert set(plan_group_tightening([richer, loser], _P)) == {2}


class TestEfficiencyOrdering:
    """A small tighten that saves a lot wins the budget over a big-but-poor one."""

    def test_high_efficiency_loser_selected_first(self):
        # Budget covers only one. Loser 3: small cushion cost, huge saving (very
        # wide initial stop). Loser 2: same cost, small saving. 3 must win.
        # Budget 13.6 € covers one floor cushion (13 €) but not two (26 €).
        winner = _winner(1, 1.7)  # guaranteed 17 €; budget 13.6 €.
        near = _loser(2, open_at=50.0, follower=48.5, bid=49.0)  # tiny saving
        far = _loser(3, open_at=50.0, follower=30.0, bid=49.0)  # huge saving
        plan = plan_group_tightening([winner, near, far], _P)
        assert set(plan) == {3}


class TestRichnessCushion:
    """Cushion width scales with how comfortably the budget covers the exposure."""

    def test_rich_budget_widens_the_cushion(self):
        loser = _loser(2, open_at=50.0, follower=30.0, bid=49.0, atr_value=2.0)
        # Thin pot still affords the floor cushion (13 €) but leaves tiny surplus.
        thin = plan_group_tightening([_winner(1, 1.65), loser], _P)  # small pot
        rich = plan_group_tightening([_winner(1, 100.0), loser], _P)  # huge pot
        # A richer pot places the stop further BELOW the bid (wider cushion).
        assert rich[2] < thin[2]

    def test_thin_budget_collapses_to_the_noise_floor(self):
        # Budget barely covers the floor exposure -> cushion == max(noise, min_dist).
        loser = _loser(
            2,
            open_at=50.0,
            follower=30.0,
            bid=49.0,
            noise=0.4,
            min_stop_distance=0.2,
            atr_value=3.0,
        )
        # Guaranteed just enough that budget ~= floor cost. floor 0.4 -> stop 48.6,
        # cost (50-48.6)*10 = 14 €; need budget ~14 -> guaranteed 17.5, follower +1.75.
        plan = plan_group_tightening([_winner(1, 1.75), loser], _P)
        assert plan[2] == pytest.approx(49.0 - 0.4, abs=1e-6)

    def test_cushion_never_below_min_stop_distance(self):
        loser = _loser(
            2,
            open_at=50.0,
            follower=30.0,
            bid=49.0,
            noise=0.05,
            min_stop_distance=0.6,
            atr_value=0.0,
        )
        # ATR 0 would give a zero cushion, but the floor is the min stop distance.
        plan = plan_group_tightening([_winner(1, 100.0), loser], _P)
        assert plan[2] == pytest.approx(49.0 - 0.6, abs=1e-6)


class TestLegality:
    """Illegal tightenings are dropped before the budget is spent."""

    def test_stop_not_above_follower_is_dropped(self):
        # Follower already tighter than any bid-cushion stop -> not a tightening.
        loser = _loser(2, open_at=50.0, follower=48.99, bid=49.0, noise=0.3)
        assert plan_group_tightening([_winner(1, 100.0), loser], _P) == {}

    def test_stop_never_at_or_above_bid(self):
        # Whatever is tightened, it must sit strictly below the live bid.
        members = [
            _winner(1, 100.0),
            _loser(2),
            _loser(3, open_at=200.0, follower=190.0, bid=199.0),
        ]
        plan = plan_group_tightening(members, _P)
        for m in members[1:]:
            if m.position_id in plan:
                assert plan[m.position_id] < m.current_price


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
        members = [_winner(1, 20.0), _loser(2)]
        assert SmartGroupStop().plan(members) == plan_group_tightening(members, _P)
