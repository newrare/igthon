"""Tests for the random diagnostic entry (src/entry/open_testing.py).

``open_testing`` is a *ranker* whose only job is to open as many different
markets per day as the wallet allows, at random, to stress-test the stops and
close zones. These tests cover the registry, the cross-epic contract, the
rolling-selection knobs (huge target + wallet gate + open-immediately), the
random BUY score, and the structural ``None`` cases (insufficient warm-up, zero
volatility).
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from src.entry import EntryIntent, OpenTesting, get_entry_strategy
from src.entry.base import EntryStrategy
from src.feed.price_buffer import Candle, EpicBuffer


def _settings(**overrides) -> SimpleNamespace:
    # Parameters are class constants, so ``from_settings`` ignores settings; this
    # stand-in only needs to exist for the registry call.
    return SimpleNamespace(**overrides)


def _buffer(n: int, spread: float = 0.5) -> EpicBuffer:
    """A gently rising, volatile buffer of ``n`` candles (positive ATR)."""
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
        strat = get_entry_strategy("open_testing", _settings())
        assert isinstance(strat, OpenTesting)

    def test_is_cross_epic_selection(self):
        assert OpenTesting.cross_epic_selection is True

    def test_is_entry_strategy_instance(self):
        assert isinstance(OpenTesting(), EntryStrategy)

    def test_rolling_selection_knobs(self):
        # The scheduler reads these off the strategy instance. A huge target means
        # the wallet — not a position cap — limits how many markets open; the
        # zero warm-up / participation means opens start as soon as epics are ready.
        strat = OpenTesting()
        assert strat.concurrent_positions >= 100
        assert strat.open_after_minutes == 0
        assert strat.min_participation_ratio == 0.0
        assert 0.0 <= strat.wallet_reserve < 1.0

    def test_warmup_never_below_atr_requirement(self):
        # ATR needs atr_period + 1 candles to be non-zero; the warm-up must honour
        # that even if warmup_candles were set lower.
        strat = OpenTesting(warmup_candles=1)
        assert strat.warmup >= strat.atr_period + 1


class TestEvaluate:
    def test_emits_buy_with_bounded_score(self):
        intent = OpenTesting().evaluate("TEST.EPIC", _buffer(30))
        assert isinstance(intent, EntryIntent)
        assert intent.direction == "BUY"
        assert 0.0 <= intent.score < 1.0

    def test_insufficient_warmup_returns_none(self):
        strat = OpenTesting()
        assert strat.evaluate("TEST.EPIC", _buffer(strat.warmup - 1)) is None

    def test_zero_volatility_returns_none(self):
        # A perfectly flat curve has no ATR — no way to size a stop, so no open.
        buf = EpicBuffer(epic="TEST.EPIC", max_candles=40)
        start = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)
        for i in range(30):
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
        assert OpenTesting().evaluate("TEST.EPIC", buf) is None

    def test_score_is_random_across_calls(self):
        # Over many evaluations the score must vary (not a constant) — that
        # randomness is what shuffles the wallet-spend order for diversity.
        strat = OpenTesting()
        buf = _buffer(30)
        scores = {strat.evaluate("TEST.EPIC", buf).score for _ in range(20)}
        assert len(scores) > 1
