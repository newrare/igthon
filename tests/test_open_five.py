"""Tests for the five-shape basket ranker (src/entry/open_five.py).

Two halves, matching the module's two responsibilities:

- ``evaluate`` is a *ranker* — it returns a comparable score for every scorable
  epic and never an exit level. Covered here: the registry, the two-sided contract
  (a clean fall scores like a clean rise), the mirrored components, the hard
  direction gate, the counter-trend malus and the structural ``None`` paths.
- ``filter_ranked`` is the cross-epic half — the duplicate-shape veto that keeps a
  basket of five from being one bet taken five times.

The selection-layer constants (``concurrent_positions = 5``,
``require_flat_book``) are asserted here as the strategy's contract; they are
exercised against the scheduler in ``tests/test_scheduler.py``
(``TestRequireFlatBook``, ``TestCrossEpicFilter``).
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from src.entry import OpenFive, get_entry_strategy
from src.entry.base import EntryIntent, EntryStrategy
from src.entry.open_five import (
    _adverse_excursion_safety,
    _counter_trend_factor,
    _directional_r2,
)
from src.feed.price_buffer import Candle, EpicBuffer


def _settings(**overrides) -> SimpleNamespace:
    # The ranker's parameters are class constants, so ``from_settings`` ignores
    # settings entirely; this stand-in only needs to exist for the registry call.
    return SimpleNamespace(**overrides)


def _buffer(
    closes: list[float],
    epic: str = "TEST.EPIC",
    spread: float = 0.5,
    pad: float = 0.1,
    start: datetime | None = None,
) -> EpicBuffer:
    """Build a buffer from bid closes, one candle per minute."""
    buf = EpicBuffer(epic=epic, max_candles=len(closes) + 10)
    stamp = start or datetime(2024, 1, 1, 9, 0, tzinfo=UTC)
    prev = closes[0]
    for close in closes:
        high = max(prev, close) + pad
        low = min(prev, close) - pad
        buf.add(
            Candle(
                timestamp=stamp,
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
        stamp += timedelta(minutes=1)
    return buf


def _rise(n: int = 80, start: float = 8000.0, step: float = 2.0) -> list[float]:
    """A clean climb — the long the ranker is built to crown."""
    return [start + i * step for i in range(n)]


def _fall(n: int = 80, start: float = 8000.0, step: float = 2.0) -> list[float]:
    """The exact mirror of :func:`_rise` — the short of identical quality."""
    return [start - i * step for i in range(n)]


def _wobbly(n: int = 80, start: float = 8000.0, drift: float = 2.0) -> list[float]:
    """A rising curve with an irregular wobble, so twins are distinguishable."""
    return [start + i * drift + (i % 7) * 1.5 - (i % 3) * 2.0 for i in range(n)]


def _independent(n: int = 80, start: float = 8000.0) -> list[float]:
    """A rise whose step rhythm is unrelated to :func:`_wobbly`."""
    return [start + i * 1.8 + (i % 5) * 3.0 - (i % 11) * 1.2 for i in range(n)]


class TestRegistryAndContract:
    def test_registered_under_its_name(self):
        strategy = get_entry_strategy("open_five", _settings())

        assert isinstance(strategy, OpenFive)
        assert strategy.name == "open_five"

    def test_is_a_cross_epic_two_sided_ranker(self):
        strategy = OpenFive()

        assert strategy.cross_epic_selection is True
        assert strategy.emits_shorts is True

    def test_opens_a_series_of_five_from_a_flat_book(self):
        """The portfolio contract: five at once, nothing until the book is empty."""
        strategy = OpenFive()

        assert strategy.concurrent_positions == 5
        # Count-bounded: five is the decision, so the wallet only ever trims it.
        assert strategy.wallet_bounded is False
        # No cooldown — the whole basket goes on in one pass.
        assert strategy.open_cooldown_minutes == 0
        assert strategy.require_flat_book is True
        # The two brakes must not be confused: only the flat-book one is used.
        assert strategy.block_open_while_alive is False

    def test_needs_a_deep_pool_to_rank_from(self):
        strategy = OpenFive()

        assert strategy.min_participation_ratio == 0.5
        # Four times the basket size, so the duplicate filter has spares.
        assert strategy.min_participation_count == 20
        assert strategy.min_participation_count >= 4 * strategy.concurrent_positions

    def test_warmup_covers_every_window_it_reads(self):
        strategy = OpenFive()

        assert strategy.warmup > strategy.signature_window
        assert strategy.warmup > strategy.regression_period_long
        assert strategy.warmup > strategy.excursion_period

    def test_weights_sum_to_one(self):
        strategy = OpenFive()
        total = (
            strategy.weight_projection
            + strategy.weight_shape
            + strategy.weight_safety
            + strategy.weight_momentum
            + strategy.weight_regime
            + strategy.weight_spread
        )

        # The exponents of a weighted geometric mean must sum to 1 for the score to
        # stay in [0, 1] and read as a percentage.
        assert abs(total - 1.0) < 1e-9

    def test_filter_ranked_is_the_base_hook(self):
        # The default hook is the identity, so every other strategy is unaffected.
        assert EntryStrategy.filter_ranked(OpenFive(), []) == []


class TestComponents:
    def test_directional_r2_ignores_a_fit_against_the_trade(self):
        rise = _rise(30)

        assert _directional_r2(rise, sign=1.0) > 0.99
        assert _directional_r2(rise, sign=-1.0) == 0.0

    def test_adverse_excursion_is_mirrored(self):
        # A climb that gives back a chunk mid-way: that retracement is adverse for
        # a long and simply the entry point for a short.
        curve = [100.0, 104.0, 108.0, 102.0, 110.0, 114.0]

        long_safety = _adverse_excursion_safety(curve, sign=1.0)
        short_safety = _adverse_excursion_safety(curve, sign=-1.0)

        # Long: worst drawdown 6 over a range of 14.
        assert abs(long_safety - (1.0 - 6.0 / 14.0)) < 1e-9
        # Short: worst run-up is the whole climb from the low, so it scores 0.
        assert short_safety == 0.0

    def test_monotone_move_is_perfectly_safe(self):
        assert _adverse_excursion_safety(_rise(40), sign=1.0) == 1.0
        assert _adverse_excursion_safety(_fall(40), sign=-1.0) == 1.0

    def test_flat_curve_has_no_safety_score(self):
        assert _adverse_excursion_safety([100.0] * 20, sign=1.0) == 0.0

    def test_counter_trend_malus_only_bites_against_the_trade(self):
        slide = [100.0 - i * 0.5 for i in range(10)]

        # A long into a clean slide is dragged down to the floor...
        against = _counter_trend_factor(slide, 1.0, 0.003, 0.05)
        # ...and the same slide is exactly what a short wants.
        with_it = _counter_trend_factor(slide, -1.0, 0.003, 0.05)

        assert against < 0.1
        assert with_it == 1.0

    def test_counter_trend_malus_is_unaffected_by_a_flat_window(self):
        assert _counter_trend_factor([100.0] * 10, 1.0, 0.003, 0.05) == 1.0


class TestEvaluate:
    def test_a_clean_rise_is_bought(self):
        strategy = OpenFive()

        intent = strategy.evaluate("E", _buffer(_rise()))

        assert intent is not None
        assert intent.direction == "BUY"
        assert 0.0 < intent.score <= 1.0

    def test_a_clean_fall_is_sold(self):
        strategy = OpenFive()

        intent = strategy.evaluate("E", _buffer(_fall()))

        assert intent is not None
        assert intent.direction == "SELL"

    def test_the_two_sides_score_symmetrically(self):
        """A mirrored fall must rank like the rise, or one side never wins.

        The components are mirrored around the direction, so the two scores agree
        to within the small asymmetry the relative measures introduce (returns and
        the spread ratio divide by prices that differ between the two curves).
        """
        strategy = OpenFive()

        rise = strategy.evaluate("E", _buffer(_rise()))
        fall = strategy.evaluate("E", _buffer(_fall()))

        assert rise is not None and fall is not None
        assert abs(rise.score - fall.score) < 0.05

    def test_a_clean_curve_outranks_a_ragged_one(self):
        strategy = OpenFive()

        clean = strategy.evaluate("E", _buffer(_rise()))
        ragged = strategy.evaluate("E", _buffer(_wobbly()))

        assert clean is not None and ragged is not None
        assert clean.score > ragged.score

    def test_disagreeing_horizons_are_vetoed(self):
        """The falling-knife guard: a session-long climb sliding into the open.

        The composite could still rank this market highly on its earlier strength,
        so the gate is a hard veto rather than a penalty — a ranker must open the
        best of its pool, and the least-bad directionless market would otherwise
        still be opened.
        """
        strategy = OpenFive()
        closes = _rise(80)
        # Undo the last 25 minutes, so the session slope is up and the recent one
        # down — no agreed direction.
        closes = closes[:-25] + [closes[-25] - i * 4.0 for i in range(25)]

        assert strategy.evaluate("E", _buffer(closes)) is None

    def test_the_gate_can_be_disabled(self):
        """With ``require_agreed_trend`` off, the side follows the session slope.

        The epic is not thereby *opened*: the same rolling-over curve is then
        refused a step later by the projection gate (almost no model still points
        up) and, failing that, ranked at the back by the counter-trend malus. What
        the flag changes is only whether the disagreement is a **veto**.
        """
        strategy = OpenFive()
        closes = _rise(80)
        closes = closes[:-25] + [closes[-25] - i * 4.0 for i in range(25)]
        bids = _buffer(closes).bid_closes

        assert strategy._direction("E", bids) is None
        strategy.require_agreed_trend = False
        assert strategy._direction("E", bids) == "BUY"

    def test_a_flat_market_is_refused(self):
        strategy = OpenFive()

        # No slope to pick a side from, and no ATR to size a stop with.
        assert strategy.evaluate("E", _buffer([8000.0] * 80)) is None

    def test_too_little_history_is_refused(self):
        strategy = OpenFive()

        assert strategy.evaluate("E", _buffer(_rise(10))) is None

    def test_a_non_positive_bid_is_refused(self):
        strategy = OpenFive()
        buf = _buffer(_rise())
        buf.candles[-1].bid_close = 0.0

        assert strategy.evaluate("E", buf) is None

    def test_the_score_floor_is_respected(self):
        strategy = OpenFive()
        strategy.min_score = 0.99

        assert strategy.evaluate("E", _buffer(_wobbly())) is None

    def test_intent_carries_no_exit_level(self):
        # The open/close decoupling: an EntryIntent is direction (+ hints) only.
        intent = OpenFive().evaluate("E", _buffer(_rise()))

        assert intent is not None
        assert set(EntryIntent.__slots__) == {"epic", "direction", "size_hint", "score"}


class TestFilterRanked:
    """The duplicate-shape veto — the reason a basket of five is five bets."""

    @staticmethod
    def _ranked(*curves: tuple[str, list[float], str]):
        return [
            (
                EntryIntent(epic=epic, direction=direction, score=1.0 - i * 0.01),
                _buffer(closes, epic=epic),
            )
            for i, (epic, closes, direction) in enumerate(curves)
        ]

    def test_a_second_listing_of_the_same_commodity_is_dropped(self):
        """The London/New York cocoa case, decided on the maths alone.

        The two curves differ only by a price scale — the epic names carry no hint
        of the relationship, and none is needed.
        """
        base = _wobbly()
        ranked = self._ranked(
            ("COCOA.LDN", base, "BUY"),
            ("COCOA.NY", [c * 0.61 for c in base], "BUY"),
            ("SOMETHING.ELSE", _independent(), "BUY"),
        )

        kept = OpenFive().filter_ranked(ranked)

        assert [intent.epic for intent, _ in kept] == ["COCOA.LDN", "SOMETHING.ELSE"]

    def test_the_better_ranked_twin_survives(self):
        base = _wobbly()
        ranked = self._ranked(
            ("SECOND.BEST", [c * 0.61 for c in base], "BUY"),
            ("BEST", base, "BUY"),
        )
        # Reversing the input order must reverse which twin is kept.
        kept = OpenFive().filter_ranked(ranked)

        assert [intent.epic for intent, _ in kept] == ["SECOND.BEST"]

    def test_a_mirrored_pair_taken_on_opposite_sides_is_dropped(self):
        """Long one leg, short its mirror: two names, one bet.

        A raw correlation reads this pair at about -1 and would let it through; the
        direction-signed redundancy turns it back into the duplicate it is.
        """
        base = _wobbly()
        mirror = [16000.0 - c for c in base]
        ranked = self._ranked(("PAIR.A", base, "BUY"), ("PAIR.B", mirror, "SELL"))

        kept = OpenFive().filter_ranked(ranked)

        assert [intent.epic for intent, _ in kept] == ["PAIR.A"]

    def test_a_hedge_on_correlated_markets_is_kept(self):
        # Opposite bets on the same shape offset each other instead of stacking
        # risk, so the filter must not treat them as duplicates.
        base = _wobbly()
        ranked = self._ranked(
            ("A", base, "BUY"), ("B", [c * 0.61 for c in base], "SELL")
        )

        kept = OpenFive().filter_ranked(ranked)

        assert [intent.epic for intent, _ in kept] == ["A", "B"]

    def test_independent_curves_all_survive(self):
        ranked = self._ranked(
            ("A", _wobbly(), "BUY"),
            ("B", _independent(), "BUY"),
            ("C", _fall(), "SELL"),
        )

        kept = OpenFive().filter_ranked(ranked)

        assert len(kept) == 3

    def test_a_dropped_candidate_promotes_the_next_survivor(self):
        """Filtering runs over the whole ranking, not just the basket slots.

        With a basket of two, the duplicate at rank 2 must not leave a hole: the
        independent curve behind it inherits the slot.
        """
        base = _wobbly()
        ranked = self._ranked(
            ("A", base, "BUY"),
            ("A.TWIN", [c * 0.61 for c in base], "BUY"),
            ("B", _independent(), "BUY"),
        )

        kept = OpenFive().filter_ranked(ranked)

        assert [intent.epic for intent, _ in kept[:2]] == ["A", "B"]

    def test_order_is_preserved(self):
        ranked = self._ranked(
            ("A", _wobbly(), "BUY"),
            ("B", _independent(), "BUY"),
            ("C", _fall(), "SELL"),
        )

        kept = OpenFive().filter_ranked(ranked)

        assert [intent.score for intent, _ in kept] == sorted(
            (intent.score for intent, _ in kept), reverse=True
        )

    def test_an_unsignable_curve_is_kept_unfiltered(self):
        # A data problem (here: a curve too short to fingerprint) must never
        # silently shrink the basket.
        ranked = self._ranked(("A", _wobbly(), "BUY"), ("SHORT", [8000.0], "BUY"))

        kept = OpenFive().filter_ranked(ranked)

        assert [intent.epic for intent, _ in kept] == ["A", "SHORT"]

    def test_a_disabled_threshold_keeps_the_twins(self):
        strategy = OpenFive()
        strategy.max_shape_redundancy = 1.0
        base = _wobbly()
        ranked = self._ranked(
            ("A", base, "BUY"), ("A.TWIN", [c * 0.61 for c in base], "BUY")
        )

        assert len(strategy.filter_ranked(ranked)) == 2

    def test_empty_ranking(self):
        assert OpenFive().filter_ranked([]) == []
