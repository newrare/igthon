"""Tests for the manual entry (src/entry/open_manual.py).

``open_manual`` is the no-op *open* side: the bot never opens automatically, the
user opens each position from the dashboard. These tests cover the registry, the
per-epic (non-ranker) contract and — the whole point — that ``evaluate`` returns
``None`` for every market state, so the analysis loop never opens on its own.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from src.entry import EntryIntent, OpenManual, get_entry_strategy
from src.entry.base import EntryStrategy
from src.feed.price_buffer import Candle, EpicBuffer


def _settings(**overrides) -> SimpleNamespace:
    # The strategy takes no parameters; this stand-in only needs to exist for the
    # registry call.
    return SimpleNamespace(**overrides)


def _buffer(n: int, spread: float = 0.5) -> EpicBuffer:
    """A gently rising, volatile buffer of ``n`` candles."""
    buf = EpicBuffer(epic="TEST.EPIC", max_candles=n + 10)
    start = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)
    prev = 8000.0
    for i in range(n):
        close = 8000.0 + i
        high = max(prev, close) + 0.5
        low = min(prev, close) - 0.5
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


class TestRegistry:
    def test_known_name_resolves(self):
        strat = get_entry_strategy("open_manual", _settings())
        assert isinstance(strat, OpenManual)

    def test_is_entry_strategy_instance(self):
        assert isinstance(OpenManual(), EntryStrategy)

    def test_is_not_cross_epic_ranker(self):
        # Per-epic strategy: the analysis loop evaluates it per epic (and gets None
        # every time) rather than routing it through the rolling ranker.
        assert OpenManual.cross_epic_selection is False

    def test_warmup_is_minimal(self):
        assert OpenManual().warmup == 1


class TestEvaluate:
    def test_never_opens_on_a_rising_market(self):
        assert OpenManual().evaluate("TEST.EPIC", _buffer(50)) is None

    def test_never_opens_on_an_empty_buffer(self):
        empty = EpicBuffer(epic="TEST.EPIC", max_candles=10)
        assert OpenManual().evaluate("TEST.EPIC", empty) is None

    def test_never_opens_across_many_states(self):
        # Whatever the market state, the manual profile stays flat — opening is the
        # user's job via the dashboard, never the analysis loop's.
        strat = OpenManual()
        results = [strat.evaluate("TEST.EPIC", _buffer(n)) for n in range(1, 40)]
        assert all(r is None for r in results)
        assert not any(isinstance(r, EntryIntent) for r in results)
