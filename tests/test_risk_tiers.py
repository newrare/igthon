"""Tests for the profit-funded risk-tier epic selection."""

import pytest

from src.markets.market_scanner import MarketInfo
from src.markets.risk_tiers import (
    DEFAULT_TIER_BOUNDS,
    RiskTier,
    select_by_risk_tier,
    tier_for_pnl,
)


def _market(
    epic: str,
    risk: float | None,
    instrument_type: str = "CURRENCIES",
    spread_ratio: float = 0.001,
) -> MarketInfo:
    """Build a minimal MarketInfo priced at ``risk`` euros per stop-out."""
    return MarketInfo(
        epic=epic,
        name=epic,
        bid=1.0,
        offer=1.0,
        spread_ratio=spread_ratio,
        dealing_enabled=True,
        status="TRADEABLE",
        instrument_type=instrument_type,
        stop_loss_eur=risk,
    )


class TestTierForPnl:
    """The unlocked band is derived from the day's banked P&L, never latched."""

    @pytest.mark.parametrize(
        ("realised", "expected"),
        [
            (0.0, (0.0, 30.0)),  # flat day — start on the small markets
            (-250.0, (0.0, 30.0)),  # a losing day never escalates
            (29.99, (0.0, 30.0)),  # the floor is exclusive
            (30.0, (0.0, 30.0)),  # exactly on the boundary: not yet unlocked
            (30.01, (30.0, 100.0)),
            (99.0, (30.0, 100.0)),
            (150.0, (100.0, 200.0)),
            (200.5, (200.0, 600.0)),
            (10_000.0, (200.0, 600.0)),  # the top rung is the last one
        ],
    )
    def test_band_follows_realised_pnl(self, realised, expected):
        tier = tier_for_pnl(realised)
        assert (tier.floor, tier.ceiling) == expected

    def test_walks_back_down_after_a_giveback(self):
        assert tier_for_pnl(250.0).index == 3
        # Same function, lower P&L: the ladder is recomputed, not remembered.
        assert tier_for_pnl(10.0).index == 0

    def test_custom_bounds(self):
        tier = tier_for_pnl(75.0, (0.0, 50.0, 500.0))
        assert (tier.index, tier.floor, tier.ceiling) == (1, 50.0, 500.0)

    def test_rejects_degenerate_bounds(self):
        with pytest.raises(ValueError):
            tier_for_pnl(0.0, (10.0,))

    def test_default_bounds_describe_four_tiers(self):
        assert len(DEFAULT_TIER_BOUNDS) - 1 == 4


class TestSelectByRiskTier:
    """Pins first, then the band, then the fills — always up to the cap."""

    def test_keeps_only_the_band_when_it_covers_the_cap(self):
        markets = [_market(f"IN{i}", 10.0) for i in range(5)]
        markets += [_market(f"OUT{i}", 400.0) for i in range(5)]

        selection = select_by_risk_tier(
            markets, cap=4, tier=RiskTier(0, 0.0, 30.0), pinned=()
        )

        assert len(selection.markets) == 4
        assert all(e.startswith("IN") for e in selection.epics)
        assert selection.from_band == 4

    def test_escalates_with_the_tier(self):
        small = [_market(f"S{i}", 10.0) for i in range(5)]
        big = [_market(f"B{i}", 150.0) for i in range(5)]

        selection = select_by_risk_tier(
            small + big, cap=3, tier=RiskTier(2, 100.0, 200.0), pinned=()
        )

        assert all(e.startswith("B") for e in selection.epics)

    def test_fills_below_the_floor_before_above_the_ceiling(self):
        markets = [
            _market("BAND", 150.0),
            _market("CHEAP_FAR", 5.0),
            _market("CHEAP_NEAR", 95.0),
            _market("DEAR", 500.0),
        ]

        selection = select_by_risk_tier(
            markets, cap=3, tier=RiskTier(2, 100.0, 200.0), pinned=()
        )

        # Band first, then the cheaper markets nearest the floor — never the
        # dearer one while anything safe is still available.
        assert selection.epics == ["BAND", "CHEAP_NEAR", "CHEAP_FAR"]
        assert (selection.from_band, selection.from_cheaper, selection.from_dearer) == (
            1,
            2,
            0,
        )

    def test_falls_back_above_the_ceiling_only_when_nothing_is_cheaper(self):
        markets = [_market("DEAR_NEAR", 220.0), _market("DEAR_FAR", 900.0)]

        selection = select_by_risk_tier(
            markets, cap=2, tier=RiskTier(0, 0.0, 30.0), pinned=()
        )

        assert selection.epics == ["DEAR_NEAR", "DEAR_FAR"]
        assert selection.from_dearer == 2

    def test_unpriced_markets_are_the_last_resort(self):
        markets = [_market("UNKNOWN", None), _market("DEAR", 900.0)]

        selection = select_by_risk_tier(
            markets, cap=2, tier=RiskTier(0, 0.0, 30.0), pinned=()
        )

        assert selection.epics == ["DEAR", "UNKNOWN"]
        assert selection.from_unknown == 1

    def test_open_positions_are_pinned_out_of_band(self):
        markets = [_market("OPEN", 500.0)] + [_market(f"IN{i}", 10.0) for i in range(5)]

        selection = select_by_risk_tier(
            markets, cap=3, tier=RiskTier(0, 0.0, 30.0), pinned={"OPEN"}
        )

        assert selection.epics[0] == "OPEN"
        assert len(selection.markets) == 3
        assert selection.pinned == 1
        # The pin took a slot from the band rather than being added on top of it.
        assert selection.from_band == 2

    def test_pins_are_never_dropped_even_over_the_cap(self):
        markets = [_market(f"OPEN{i}", 500.0) for i in range(3)]
        markets.append(_market("SMALL", 5.0))

        selection = select_by_risk_tier(
            markets,
            cap=2,
            tier=RiskTier(0, 0.0, 30.0),
            pinned={"OPEN0", "OPEN1", "OPEN2"},
        )

        assert selection.epics == ["OPEN0", "OPEN1", "OPEN2"]

    def test_stays_diversified_inside_the_band(self):
        markets = [
            _market(f"FX{i}", 10.0, "CURRENCIES", spread_ratio=0.0001 * i)
            for i in range(5)
        ]
        markets += [_market("IDX", 10.0, "INDICES", spread_ratio=0.05)]
        markets += [_market("CMD", 10.0, "COMMODITIES", spread_ratio=0.09)]

        selection = select_by_risk_tier(
            markets, cap=3, tier=RiskTier(0, 0.0, 30.0), pinned=()
        )

        classes = {m.instrument_type for m in selection.markets}
        # One market of every class, not the three tightest FX spreads.
        assert classes == {"CURRENCIES", "INDICES", "COMMODITIES"}

    def test_cap_zero_disables_the_selection(self):
        markets = [_market("A", 10.0), _market("B", 900.0)]

        selection = select_by_risk_tier(
            markets, cap=0, tier=RiskTier(0, 0.0, 30.0), pinned={"B"}
        )

        assert selection.epics == ["B", "A"]
