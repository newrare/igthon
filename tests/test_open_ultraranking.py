"""Tests for the regime-vetoing cross-epic ranker (src/entry/open_ultraranking.py).

``open_ultraranking`` is ``open_saferanking`` plus one rule: an epic whose path
over the last ``regime_period`` candles is not directional is dropped before any
scoring. These tests cover the registry and inheritance contract, the veto itself
(both sides of the threshold, and that it is *hard* rather than a score penalty),
that everything the base ranker accepts still passes through untouched, and the
warm-up arithmetic the veto's own window imposes.
"""

import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from src.core.indicators import efficiency_ratio
from src.entry import EntryIntent, OpenUltraRanking, get_entry_strategy
from src.entry.base import EntryStrategy
from src.entry.open_saferanking import OpenSafeRanking
from src.feed.price_buffer import Candle, EpicBuffer

_START = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)


def _settings(**overrides) -> SimpleNamespace:
    # Parameters are class constants, so ``from_settings`` ignores settings; this
    # stand-in only needs to exist for the registry call.
    return SimpleNamespace(**overrides)


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


def _clean_climb(n: int = 90, start: float = 8000.0, step: float = 1.0) -> list[float]:
    """A straight rise — ER ≈ 1, every dimension of the base ranker satisfied."""
    return [start + i * step for i in range(n)]


def _oscillation(n: int = 90, start: float = 8000.0, amp: float = 20.0) -> list[float]:
    """The Hang Seng shape: a wide band travelled hard for no net move (ER ≈ 0)."""
    return [start + amp * math.sin(2 * math.pi * i / 12) for i in range(n)]


def _drifting_chop(
    n: int = 90, start: float = 8000.0, amp: float = 20.0, step: float = 0.15
) -> list[float]:
    """Chop with a faint positive drift — rises without going anywhere.

    The shape the base ranker cannot refuse: the slope is up, so its trend gate
    passes and its soft efficiency term merely ranks it low.
    """
    return [start + step * i + amp * math.sin(2 * math.pi * i / 12) for i in range(n)]


class TestRegistryAndContract:
    def test_known_name_resolves(self):
        strategy = get_entry_strategy("open_ultraranking", _settings())
        assert isinstance(strategy, OpenUltraRanking)

    def test_is_an_entry_strategy(self):
        assert isinstance(OpenUltraRanking(), EntryStrategy)

    def test_extends_saferanking_rather_than_copying_it(self):
        assert isinstance(OpenUltraRanking(), OpenSafeRanking)

    def test_inherits_the_rolling_selection_model(self):
        strategy = OpenUltraRanking()
        assert strategy.cross_epic_selection is True
        assert strategy.wallet_bounded is True

    def test_stays_long_only(self):
        assert OpenUltraRanking().emits_shorts is False

    def test_warmup_covers_the_veto_window(self):
        # ``efficiency_ratio`` consumes ``period + 1`` values.
        strategy = OpenUltraRanking(regime_period=300)
        assert strategy.warmup >= 301

    def test_warmup_never_shrinks_below_the_base(self):
        strategy = OpenUltraRanking(regime_period=1)
        assert strategy.warmup == OpenSafeRanking().warmup


class TestRegimeVeto:
    def test_directionless_market_is_refused(self):
        buf = _buffer(_oscillation(200))
        assert OpenUltraRanking().evaluate("TEST.EPIC", buf) is None

    def test_rising_chop_is_refused_though_the_base_ranks_it(self):
        # The reason this strategy exists: a positive drift buried in a wide band
        # clears the base ranker's *directional* checks, and only a hard regime
        # veto can drop it.
        buf = _buffer(_drifting_chop(200))
        strategy = OpenUltraRanking()
        measured = efficiency_ratio(buf.mid_closes, strategy.regime_period)
        assert measured < strategy.min_regime_efficiency
        assert strategy.evaluate("TEST.EPIC", buf) is None

    def test_clean_trend_survives_the_veto(self):
        buf = _buffer(_clean_climb(200))
        strategy = OpenUltraRanking()
        measured = efficiency_ratio(buf.mid_closes, strategy.regime_period)
        assert measured >= strategy.min_regime_efficiency
        assert strategy.evaluate("TEST.EPIC", buf) is not None

    def test_veto_is_hard_not_a_score_penalty(self):
        # Same curve, threshold either side of what it measures: the outcome flips
        # between an intent and nothing at all, never a merely lower score.
        buf = _buffer(_clean_climb(200))
        measured = efficiency_ratio(buf.mid_closes, 60)
        assert (
            OpenUltraRanking(min_regime_efficiency=measured - 0.01).evaluate(
                "TEST.EPIC", buf
            )
            is not None
        )
        assert (
            OpenUltraRanking(min_regime_efficiency=measured + 0.01).evaluate(
                "TEST.EPIC", buf
            )
            is None
        )

    def test_a_disabled_threshold_defers_entirely_to_the_base(self):
        buf = _buffer(_clean_climb(200))
        assert OpenUltraRanking(min_regime_efficiency=0.0).evaluate(
            "TEST.EPIC", buf
        ) == OpenSafeRanking().evaluate("TEST.EPIC", buf)

    def test_short_buffer_returns_none(self):
        buf = _buffer(_clean_climb(10))
        assert OpenUltraRanking().evaluate("TEST.EPIC", buf) is None

    def test_empty_buffer_returns_none(self):
        buf = EpicBuffer(epic="TEST.EPIC", max_candles=10)
        assert OpenUltraRanking().evaluate("TEST.EPIC", buf) is None


class TestSurvivorsAreUnchanged:
    def test_accepted_intent_matches_the_base_ranker_exactly(self):
        # The veto only removes candidates; it must not touch the ranking of the
        # ones it lets through, or scores stop being comparable across strategies.
        buf = _buffer(_clean_climb(200))
        ultra = OpenUltraRanking().evaluate("TEST.EPIC", buf)
        safe = OpenSafeRanking().evaluate("TEST.EPIC", buf)
        assert ultra is not None and safe is not None
        assert ultra.direction == safe.direction
        assert ultra.score == safe.score

    def test_intent_is_a_buy_with_a_unit_interval_score(self):
        buf = _buffer(_clean_climb(200))
        intent = OpenUltraRanking().evaluate("TEST.EPIC", buf)
        assert isinstance(intent, EntryIntent)
        assert intent.epic == "TEST.EPIC"
        assert intent.direction == "BUY"
        assert 0.0 <= intent.score <= 1.0

    def test_base_rejections_still_reject(self):
        # A clean *fall* is refused by the inherited trend gate, not by the veto:
        # the ER is high, so the veto passes and the base must still say no.
        buf = _buffer(_clean_climb(200, start=9000.0, step=-1.0))
        strategy = OpenUltraRanking()
        assert (
            efficiency_ratio(buf.mid_closes, strategy.regime_period)
            >= strategy.min_regime_efficiency
        )
        assert strategy.evaluate("TEST.EPIC", buf) is None
