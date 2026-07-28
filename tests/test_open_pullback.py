"""Tests for the pull-back ranker (src/entry/open_pullback.py).

``open_pullback`` is a two-sided *ranker* reverse-engineered from the manual
opens of the 2026-07-24 session: join a clean, strong trend during a pause,
below the extreme rather than at it. These tests cover the registry, the
two-sided direction contract, the score contract, the structural ``None`` cases,
and each hard gate — trend size, cleanliness, mid-range continuation, the pause
(the distinctive half of the signature) and the distance band to the extreme.

The ``emits_shorts`` contract is asserted here and exercised end-to-end against
the scheduler in ``tests/test_scheduler.py``.
"""

from dataclasses import fields
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.entry import OpenPullback, get_entry_strategy
from src.entry.base import EntryStrategy
from src.entry.open_pullback import _tent
from src.feed.price_buffer import Candle, EpicBuffer

EPIC = "CC.D.CC.UNC.IP"


def _settings(**overrides) -> SimpleNamespace:
    # The ranker's parameters are class constants, so ``from_settings`` ignores
    # settings entirely; this stand-in only needs to exist for the registry call.
    return SimpleNamespace(**overrides)


def _buffer(closes: list[float], spread: float = 0.5, pad: float = 1.5) -> EpicBuffer:
    """Build a buffer from bid closes, with ±``pad`` intra-candle high/low."""
    buf = EpicBuffer(epic=EPIC, max_candles=len(closes) + 10)
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


def _up_then_pause(rise: float = 1.5, dip: float = 0.3) -> list[float]:
    """Textbook long setup: a clean 75-candle climb, then a 15-candle drift back.

    The pull-back leaves the bid a couple of ATRs below the channel high — inside
    the allowed distance band and not at the extreme.
    """
    closes = [5400.0 + i * rise for i in range(75)]
    top = closes[-1]
    closes += [top - (i + 1) * dip for i in range(15)]
    return closes


def _down_then_pause(fall: float = 1.5, pop: float = 0.3) -> list[float]:
    """Mirror of :func:`_up_then_pause` — a clean fall, then a small bounce."""
    closes = [5400.0 - i * fall for i in range(75)]
    bottom = closes[-1]
    closes += [bottom + (i + 1) * pop for i in range(15)]
    return closes


# --- registry / contract -------------------------------------------------


def test_registered_under_its_name() -> None:
    strategy = get_entry_strategy("open_pullback", _settings())
    assert isinstance(strategy, OpenPullback)
    assert isinstance(strategy, EntryStrategy)
    assert strategy.name == "open_pullback"


def test_selection_layer_contract() -> None:
    """The knobs the scheduler reads are part of this strategy's contract."""
    s = OpenPullback()
    assert s.cross_epic_selection is True
    assert s.emits_shorts is True  # two-sided: the scheduler must keep SELLs
    assert s.wallet_bounded is True
    # Same-day re-open is a GLOBAL .env policy (ALLOW_SAME_DAY_REOPEN), not a
    # strategy knob: a class attribute here would be silently ignored.
    assert not hasattr(s, "allow_same_day_reopen")
    assert s.open_cooldown_minutes > 0


# --- direction: symmetric by construction --------------------------------


def test_pullback_in_uptrend_is_bought() -> None:
    intent = OpenPullback().evaluate(EPIC, _buffer(_up_then_pause()))
    assert intent is not None
    assert intent.direction == "BUY"


def test_pullback_in_downtrend_is_sold() -> None:
    intent = OpenPullback().evaluate(EPIC, _buffer(_down_then_pause()))
    assert intent is not None
    assert intent.direction == "SELL"


@pytest.mark.parametrize("closes", [_up_then_pause(), _down_then_pause()])
def test_score_is_a_unit_interval_ranking_figure(closes: list[float]) -> None:
    intent = OpenPullback().evaluate(EPIC, _buffer(closes))
    assert intent is not None
    assert 0.0 <= intent.score <= 1.0


def test_intent_carries_no_exit_level() -> None:
    """Open/close decoupling: an intent is direction + score, nothing else."""
    intent = OpenPullback().evaluate(EPIC, _buffer(_up_then_pause()))
    assert intent is not None
    assert {f.name for f in fields(intent)} == {
        "epic",
        "direction",
        "size_hint",
        "score",
    }


# --- structural None ------------------------------------------------------


def test_none_before_warmup() -> None:
    strategy = OpenPullback()
    assert strategy.evaluate(EPIC, _buffer([5400.0] * (strategy.warmup - 1))) is None


def test_none_on_flat_market_without_volatility() -> None:
    assert OpenPullback().evaluate(EPIC, _buffer([5400.0] * 100, pad=0.0)) is None


# --- hard gates -----------------------------------------------------------


def test_rejects_trend_too_weak() -> None:
    weak = [5400.0 + i * 0.002 for i in range(75)] + [5400.15] * 15
    assert OpenPullback().evaluate(EPIC, _buffer(weak)) is None


def test_rejects_dirty_trend() -> None:
    """A zig-zag has no linear trend to join: the R² gate must drop it."""
    choppy = [5400.0 + (25.0 if i % 2 else -25.0) for i in range(90)]
    assert OpenPullback().evaluate(EPIC, _buffer(choppy)) is None


def test_rejects_extended_short_leg() -> None:
    """Price still thrusting at the extreme is a breakout, not a pull-back."""
    thrusting = [5400.0 + i * 1.2 for i in range(90)]
    assert OpenPullback().evaluate(EPIC, _buffer(thrusting)) is None


def test_rejects_when_no_pause_occurred() -> None:
    """The ``roc`` gate is the distinctive half of the signature."""
    strategy = OpenPullback(max_roc_pct=-99.0)  # unsatisfiable pause requirement
    assert strategy.evaluate(EPIC, _buffer(_up_then_pause())) is None


def test_rejects_pullback_that_went_too_deep() -> None:
    """Beyond the band the trend has broken, not paused."""
    deep = _up_then_pause(dip=6.0)
    assert OpenPullback().evaluate(EPIC, _buffer(deep)) is None


def test_rejects_when_mid_range_move_has_stalled() -> None:
    """A trend that died 30 candles ago is a memory, not a live move."""
    stalled = [5400.0 + i * 2.0 for i in range(60)] + [5518.0] * 30
    assert OpenPullback().evaluate(EPIC, _buffer(stalled)) is None


def test_rejects_instrument_that_barely_moves() -> None:
    strategy = OpenPullback(min_atr_pct=5.0)  # unreachable floor
    assert strategy.evaluate(EPIC, _buffer(_up_then_pause())) is None


# --- ranking behaviour ----------------------------------------------------


def test_stronger_trend_outranks_weaker() -> None:
    """Dip held constant so both land at the same distance from the extreme:
    the only thing separating them is the strength of the trend joined."""
    strategy = OpenPullback()
    strong = strategy.evaluate(EPIC, _buffer(_up_then_pause(rise=3.0, dip=0.7)))
    weak = strategy.evaluate(EPIC, _buffer(_up_then_pause(rise=1.8, dip=0.7)))
    assert strong is not None and weak is not None
    assert strong.score > weak.score


def test_wider_spread_ranks_lower() -> None:
    tight = OpenPullback().evaluate(EPIC, _buffer(_up_then_pause(), spread=0.1))
    wide = OpenPullback().evaluate(EPIC, _buffer(_up_then_pause(), spread=7.0))
    assert tight is not None and wide is not None
    assert tight.score > wide.score


def test_tent_peaks_at_the_middle_of_the_band() -> None:
    assert _tent(2.0, 0.5, 3.5) == pytest.approx(1.0)
    assert _tent(0.5, 0.5, 3.5) == 0.0
    assert _tent(3.5, 0.5, 3.5) == 0.0
    assert 0.0 < _tent(1.0, 0.5, 3.5) < 1.0
