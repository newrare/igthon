"""Tests for the fade ranker (src/entry/open_fade.py).

``open_fade`` is a *ranker* (``cross_epic_selection``) and — unlike every entry
that came before it — a genuinely **two-sided** one: it buys a clean fall that
has reached the bottom of its channel and sells a clean rise that has reached
the top. These tests cover the registry, the two-sided direction contract, the
score contract ([0, 1]), the structural ``None`` cases, and each hard gate:
trend size, trend cleanliness, channel position, volatility and the commodity
restriction.

The ``emits_shorts`` contract is asserted here and exercised end-to-end against
the scheduler in ``tests/test_scheduler.py``.
"""

from dataclasses import fields
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.entry import OpenFade, get_entry_strategy
from src.entry.base import EntryStrategy
from src.feed.price_buffer import Candle, EpicBuffer

COMMODITY = "CC.D.CC.UNC.IP"
FOREX = "CS.D.EURUSD.CFD.IP"


def _settings(**overrides) -> SimpleNamespace:
    # The ranker's parameters are class constants, so ``from_settings`` ignores
    # settings entirely; this stand-in only needs to exist for the registry call.
    return SimpleNamespace(**overrides)


def _buffer(
    closes: list[float], epic: str = COMMODITY, spread: float = 0.5, pad: float = 1.5
) -> EpicBuffer:
    """Build a buffer from bid closes, with ±``pad`` intra-candle high/low."""
    buf = EpicBuffer(epic=epic, max_candles=len(closes) + 10)
    start = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)
    prev = closes[0]
    for i, close in enumerate(closes):
        high = max(prev, close) + pad
        low = min(prev, close) - pad
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


def _falling(n: int = 90, start: float = 5400.0, step: float = 1.0) -> list[float]:
    """A clean, steady fall that ends at the low of its own channel -> BUY."""
    return [start - i * step for i in range(n)]


def _rising(n: int = 90, start: float = 5400.0, step: float = 1.0) -> list[float]:
    """A clean, steady rise that ends at the high of its own channel -> SELL."""
    return [start + i * step for i in range(n)]


def _choppy(n: int = 90, base: float = 5400.0) -> list[float]:
    """Directionless oscillation: a trend too weak/dirty to be worth fading."""
    return [base + (12.0 if i % 2 else -12.0) for i in range(n)]


# --- registry / contract -------------------------------------------------


def test_registered_under_its_name() -> None:
    strategy = get_entry_strategy("open_fade", _settings())
    assert isinstance(strategy, OpenFade)
    assert isinstance(strategy, EntryStrategy)
    assert strategy.name == "open_fade"


def test_selection_layer_contract() -> None:
    """The knobs the scheduler reads are part of this strategy's contract."""
    s = OpenFade()
    assert s.cross_epic_selection is True
    assert s.emits_shorts is True  # two-sided: the scheduler must keep SELLs
    assert s.wallet_bounded is True
    # Same-day re-open is a GLOBAL .env policy (ALLOW_SAME_DAY_REOPEN), not a
    # strategy knob: a class attribute here would be silently ignored.
    assert not hasattr(s, "allow_same_day_reopen")
    assert s.open_cooldown_minutes > 0  # correlated commodities must be spaced


# --- direction: the two-sided core --------------------------------------


def test_clean_fall_at_channel_low_is_bought() -> None:
    intent = OpenFade().evaluate(COMMODITY, _buffer(_falling()))
    assert intent is not None
    assert intent.direction == "BUY"


def test_clean_rise_at_channel_high_is_sold() -> None:
    intent = OpenFade().evaluate(COMMODITY, _buffer(_rising()))
    assert intent is not None
    assert intent.direction == "SELL"


@pytest.mark.parametrize("closes", [_falling(), _rising()])
def test_score_is_a_unit_interval_ranking_figure(closes: list[float]) -> None:
    intent = OpenFade().evaluate(COMMODITY, _buffer(closes))
    assert intent is not None
    assert 0.0 <= intent.score <= 1.0


def test_intent_carries_no_exit_level() -> None:
    """Open/close decoupling: an intent is direction + score, nothing else."""
    intent = OpenFade().evaluate(COMMODITY, _buffer(_falling()))
    assert intent is not None
    assert {f.name for f in fields(intent)} == {
        "epic",
        "direction",
        "size_hint",
        "score",
    }


# --- structural None ------------------------------------------------------


def test_none_before_warmup() -> None:
    strategy = OpenFade()
    short = _falling(n=strategy.warmup - 1)
    assert strategy.evaluate(COMMODITY, _buffer(short)) is None


def test_none_on_flat_market_without_volatility() -> None:
    """A perfectly flat curve has no ATR, so no stop can be sized at open."""
    assert OpenFade().evaluate(COMMODITY, _buffer([5400.0] * 90, pad=0.0)) is None


# --- hard gates -----------------------------------------------------------


def test_rejects_trend_too_small_to_fade() -> None:
    """A drift below ``min_trend_pct`` is not an extended move."""
    tiny = [5400.0 - i * 0.002 for i in range(90)]
    assert OpenFade().evaluate(COMMODITY, _buffer(tiny)) is None


def test_rejects_dirty_trend() -> None:
    """Chop mean-reverts by accident, not by stretch: R² gate must drop it."""
    assert OpenFade().evaluate(COMMODITY, _buffer(_choppy())) is None


def test_rejects_when_not_at_the_channel_extreme() -> None:
    """A fall that has already bounced back mid-channel is no longer stretched."""
    closes = _falling(n=80) + [5320.0 + i * 3.0 for i in range(20)]
    assert OpenFade().evaluate(COMMODITY, _buffer(closes)) is None


def test_rejects_instrument_that_barely_moves() -> None:
    """ATR/price under ``min_atr_pct``: the stop cannot clear the noise."""
    strategy = OpenFade(min_atr_pct=5.0)  # unreachable floor
    assert strategy.evaluate(COMMODITY, _buffer(_falling())) is None


def test_commodity_restriction_is_on_by_default() -> None:
    """The out-of-sample edge only held on commodities — non-commodities drop."""
    buf = _buffer(_falling(), epic=FOREX)
    assert OpenFade().evaluate(FOREX, buf) is None
    assert OpenFade(commodity_only=False).evaluate(FOREX, buf) is not None


# --- ranking behaviour ----------------------------------------------------


def test_deeper_stretch_outranks_shallower() -> None:
    """The score must order candidates the way the setup's logic does."""
    strategy = OpenFade()
    deep = strategy.evaluate(COMMODITY, _buffer(_falling(step=2.0)))
    shallow = strategy.evaluate(COMMODITY, _buffer(_falling(step=0.35)))
    assert deep is not None and shallow is not None
    assert deep.score > shallow.score


def test_wider_spread_ranks_lower() -> None:
    tight = OpenFade().evaluate(COMMODITY, _buffer(_falling(), spread=0.1))
    wide = OpenFade().evaluate(COMMODITY, _buffer(_falling(), spread=7.0))
    assert tight is not None and wide is not None
    assert tight.score > wide.score
