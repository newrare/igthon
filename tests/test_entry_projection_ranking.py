"""Tests for the hourly cross-epic projection ranker
(src/entry/projection_ranking.py).

This entry is a *ranker*: ``evaluate`` returns a comparable composite score for
every scorable epic (the scheduler does the cross-epic selection). These tests
cover the registry, the settings mapping, the score contract ([0, 1], BUY-only),
the structural ``None`` cases, the ``min_score`` floor, and the ranking sanity
that a clean up-trend outscores chop.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from src.entry import EntryIntent, ProjectionRankingEntry, get_entry_strategy
from src.entry.base import EntryStrategy
from src.feed.price_buffer import Candle, EpicBuffer


def _settings(**overrides) -> SimpleNamespace:
    # The ranker's parameters are class constants, so ``from_settings`` ignores
    # settings entirely; this stand-in only needs to exist for the registry call.
    return SimpleNamespace(**overrides)


def _buffer(closes: list[float], spread: float = 0.5) -> EpicBuffer:
    buf = EpicBuffer(epic="TEST.EPIC", max_candles=len(closes) + 10)
    start = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)
    prev = closes[0]
    for i, close in enumerate(closes):
        high = max(prev, close) + 0.1
        low = min(prev, close) - 0.1
        buf.add(
            Candle(
                timestamp=start + timedelta(minutes=i),
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


def _trending_up(n: int = 80, start: float = 8000.0, step: float = 1.0) -> list[float]:
    return [start + i * step for i in range(n)]


def _choppy(n: int = 80, start: float = 8000.0, amp: float = 5.0) -> list[float]:
    # Alternating up/down with no net drift: low ER, ~flat slope, weak projection.
    return [start + (amp if i % 2 else 0.0) for i in range(n)]


class TestRegistry:
    def test_known_name_resolves(self):
        strat = get_entry_strategy("projection_ranking", _settings())
        assert isinstance(strat, ProjectionRankingEntry)

    def test_is_cross_epic_selection(self):
        assert ProjectionRankingEntry.cross_epic_selection is True

    def test_is_entry_strategy_instance(self):
        assert isinstance(ProjectionRankingEntry(), EntryStrategy)

    def test_from_settings_uses_class_constants(self):
        # Parameters are class constants now: from_settings ignores settings and
        # builds from the dataclass defaults (and the rolling-selection knobs).
        strat = get_entry_strategy("projection_ranking", _settings())
        assert isinstance(strat, ProjectionRankingEntry)
        assert strat.projection_horizon == 60
        assert strat.weight_projection == 0.40
        assert strat.min_score == 0.0

    def test_rolling_selection_constants_on_class(self):
        # The scheduler reads these off the strategy instance, not settings.
        strat = ProjectionRankingEntry()
        assert strat.concurrent_positions == 1
        assert strat.open_after_minutes == 60
        assert strat.wallet_reserve == 0.10


class TestEvaluate:
    def test_rising_curve_emits_buy_with_bounded_score(self):
        intent = ProjectionRankingEntry().evaluate("TEST.EPIC", _buffer(_trending_up()))
        assert isinstance(intent, EntryIntent)
        assert intent.direction == "BUY"
        assert 0.0 <= intent.score <= 1.0
        # A clean up-trend should score well above the floor.
        assert intent.score > 0.5

    def test_insufficient_warmup_returns_none(self):
        # warmup = max(60, 30, 10, 30, 14) + 1 = 61; 40 candles is too few.
        assert (
            ProjectionRankingEntry().evaluate("TEST.EPIC", _buffer(_trending_up(40)))
            is None
        )

    def test_zero_volatility_returns_none(self):
        # A perfectly flat curve (zero true range) has no ATR — no way to size a
        # stop at open, so the strategy refuses to score it.
        buf = EpicBuffer(epic="TEST.EPIC", max_candles=90)
        start = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)
        for i in range(80):
            buf.add(
                Candle(
                    timestamp=start + timedelta(minutes=i),
                    bid_open=8000.0,
                    bid_close=8000.0,
                    bid_high=8000.0,
                    bid_low=8000.0,
                    offer_open=8000.5,
                    offer_close=8000.5,
                    offer_high=8000.5,
                    offer_low=8000.5,
                )
            )
        assert ProjectionRankingEntry().evaluate("TEST.EPIC", buf) is None

    def test_min_score_floor_blocks_weak_setup(self):
        # Demand a composite the chop cannot supply → stay flat.
        strat = ProjectionRankingEntry(min_score=1.01)
        assert strat.evaluate("TEST.EPIC", _buffer(_choppy())) is None

    def test_uptrend_outranks_chop(self):
        strat = ProjectionRankingEntry()
        up = strat.evaluate("TEST.EPIC", _buffer(_trending_up()))
        chop = strat.evaluate("TEST.EPIC", _buffer(_choppy()))
        assert up is not None and chop is not None
        # The ranking key: a clean up-trend must score higher than chop.
        assert up.score > chop.score
