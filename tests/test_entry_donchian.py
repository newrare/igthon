"""Tests for the decoupled Donchian *entry* strategy (src/entry/donchian_er.py).

The entry side decides only direction: it returns an ``EntryIntent`` and
crucially carries **no exit levels** — that is the whole point of the open/close
decoupling. These tests exercise the gates (spread, efficiency regime) and the
breakout logic with no close profile involved.
"""

from dataclasses import fields
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from src.entry import DonchianEntry, EntryIntent, get_entry_strategy
from src.entry.base import EntryStrategy
from src.services.price_buffer import Candle, EpicBuffer


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "strategy_donchian_channel": 20,
        "strategy_atr_period": 14,
        "strategy_efficiency_period": 30,
        "strategy_min_efficiency": 0.45,
        "strategy_max_spread_ratio": 0.0015,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


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


def _trending_up(n: int = 60, start: float = 8000.0, step: float = 1.0) -> list[float]:
    return [start + i * step for i in range(n)]


def _choppy(n: int = 60, base: float = 8000.0, amp: float = 2.0) -> list[float]:
    return [base + (amp if i % 2 else -amp) for i in range(n)]


class TestRegistry:
    def test_known_name_resolves(self):
        strat = get_entry_strategy("donchian_er", _settings())
        assert isinstance(strat, DonchianEntry)

    def test_unknown_name_raises(self):
        try:
            get_entry_strategy("nope", _settings())
        except ValueError as exc:
            assert "Unknown entry strategy" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected ValueError")

    def test_from_settings_maps_parameters(self):
        strat = get_entry_strategy(
            "donchian_er",
            _settings(strategy_donchian_channel=33, strategy_min_efficiency=0.6),
        )
        assert strat.channel == 33
        assert strat.min_efficiency == 0.6


class TestEntryIntentIsExitAgnostic:
    def test_intent_has_no_exit_levels(self):
        """The intent dataclass must not leak any exit/level field."""
        names = {f.name for f in fields(EntryIntent)}
        assert names == {"epic", "direction", "size_hint", "score"}
        # No field even mentions a level/stop/win/follower.
        assert not any(
            tok in n for n in names for tok in ("level", "stop", "win", "follower")
        )


class TestEvaluate:
    def test_breakout_in_clean_uptrend_emits_buy(self):
        intent = DonchianEntry().evaluate("TEST.EPIC", _buffer(_trending_up(60)))
        assert isinstance(intent, EntryIntent)
        assert intent.direction == "BUY"
        assert intent.score > 0  # efficiency ratio surfaced for diagnostics

    def test_choppy_regime_is_rejected_by_efficiency_gate(self):
        strat = DonchianEntry(min_efficiency=0.45)
        assert strat.evaluate("TEST.EPIC", _buffer(_choppy(60))) is None

    def test_wide_spread_is_rejected(self):
        # spread/bid far above max_spread_ratio → no entry regardless of trend.
        buf = _buffer(_trending_up(60), spread=100.0)
        assert DonchianEntry().evaluate("TEST.EPIC", buf) is None

    def test_insufficient_warmup_returns_none(self):
        assert DonchianEntry().evaluate("TEST.EPIC", _buffer(_trending_up(5))) is None

    def test_no_breakout_inside_band_returns_none(self):
        # Flat series: the last close never escapes the prior band.
        flat = [8000.0] * 60
        assert DonchianEntry().evaluate("TEST.EPIC", _buffer(flat)) is None

    def test_is_entry_strategy_instance(self):
        assert isinstance(DonchianEntry(), EntryStrategy)
