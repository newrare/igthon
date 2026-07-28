"""Tests for the rebound ranker (src/entry/open_rebound.py).

Like its siblings ``open_ranking`` / ``open_saferanking`` / ``open_allincrease``
this entry is a *ranker*: ``evaluate`` returns a comparable composite score in
[0, 1] for every scorable epic (the scheduler does the cross-epic selection).
These tests cover the registry, the score contract ([0, 1], BUY-only), the
structural ``None`` cases, and the three hard gates that define the setup — a
bullish day, a genuine sharp drop, and a recovery already under way.

The selection-layer knobs (``wallet_bounded``, ``open_cooldown_minutes``) are
asserted here as the strategy's contract and
exercised end-to-end against the scheduler in ``tests/test_scheduler.py``.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from src.entry import OpenRebound, get_entry_strategy
from src.entry.base import EntryStrategy
from src.entry.open_rebound import _tent
from src.feed.price_buffer import Candle, EpicBuffer


def _settings(**overrides) -> SimpleNamespace:
    # The ranker's parameters are class constants, so ``from_settings`` ignores
    # settings entirely; this stand-in only needs to exist for the registry call.
    return SimpleNamespace(**overrides)


def _buffer(closes: list[float], spread: float = 0.5, pad: float = 0.1) -> EpicBuffer:
    """Build a buffer from bid closes, with ±``pad`` intra-candle high/low."""
    buf = EpicBuffer(epic="TEST.EPIC", max_candles=len(closes) + 10)
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


def _rebound() -> list[float]:
    """A textbook setup: bullish day, sharp drop, mid-recovery at the end.

    120 one-minute candles. The first 60 (outside the ~60-candle dip window)
    establish a rising day; the last 60 (the dip window) climb to a peak, drop
    sharply, then recover part-way — the current bid sits mid-rebound, below the
    pre-drop peak.
    """
    closes: list[float] = []
    for i in range(120):
        if i < 60:  # rising day-base, before the dip window
            closes.append(8000.0 + i * 1.0)  # 8000 → 8059
        elif i < 80:  # still climbing to the pre-drop peak
            closes.append(8060.0 + (i - 60) * 1.0)  # 8060 → 8079, peak 8080-ish
        elif i < 95:  # the sharp drop (forte chute)
            closes.append(8080.0 - (i - 79) * 3.0)  # 8080 → 8035
        else:  # the rebound (le marché remonte), ends part-way up
            closes.append(8035.0 + (i - 94) * 0.8)  # 8035 → ~8055
    return closes


def _falling_day(n: int = 120, start: float = 8100.0, step: float = 1.0) -> list[float]:
    return [start - i * step for i in range(n)]


def _steady_rise(n: int = 120, start: float = 8000.0, step: float = 1.0) -> list[float]:
    # Monotonic climb: a bullish day with no drop at all.
    return [start + i * step for i in range(n)]


def _still_falling() -> list[float]:
    """Bullish day and a sharp drop, but the recent leg is still falling."""
    closes: list[float] = []
    for i in range(120):
        if i < 80:  # rising day-base up to the peak
            closes.append(8000.0 + i * 1.0)
        else:  # dropping all the way to the end — no recovery yet
            closes.append(8080.0 - (i - 79) * 2.0)
    return closes


class TestRegistry:
    def test_registered_under_its_name(self):
        strat = get_entry_strategy("open_rebound", _settings())
        assert isinstance(strat, OpenRebound)
        assert strat.name == "open_rebound"

    def test_is_a_cross_epic_ranker(self):
        strat = OpenRebound()
        assert strat.cross_epic_selection is True
        assert isinstance(strat, EntryStrategy)


class TestSelectionKnobs:
    """The class constants the scheduler's rolling selector reads (the spec)."""

    def test_wallet_bounded_paced_and_rotating(self):
        strat = OpenRebound()
        assert strat.wallet_bounded is True  # open while the wallet allows
        assert strat.open_cooldown_minutes == 5  # wait 5 min between opens
        # Rotating across markets is now the global .env policy
        # (ALLOW_SAME_DAY_REOPEN=false), not a strategy attribute.
        assert not hasattr(strat, "allow_same_day_reopen")

    def test_composite_weights_sum_to_one(self):
        strat = OpenRebound()
        total = (
            strat.weight_trend
            + strat.weight_drop
            + strat.weight_rebound
            + strat.weight_recency
            + strat.weight_spread
        )
        assert abs(total - 1.0) < 1e-9  # stays a [0, 1] score / percentage


class TestScoreContract:
    def test_rebound_setup_buys_with_valid_score(self):
        strat = OpenRebound()
        intent = strat.evaluate("E", _buffer(_rebound()))
        assert intent is not None
        assert intent.direction == "BUY"
        assert 0.0 <= intent.score <= 1.0
        assert intent.score >= strat.min_score


class TestHardGates:
    def test_falling_day_stays_flat(self):
        # Gate 1: the whole-session slope is negative -> not a rebound candidate.
        strat = OpenRebound()
        assert strat.evaluate("E", _buffer(_falling_day())) is None

    def test_steady_rise_without_a_drop_stays_flat(self):
        # Gate 3: a monotonic climb has no peak->trough drop -> not a rebound
        # setup (that is open_allincrease's job).
        strat = OpenRebound()
        assert strat.evaluate("E", _buffer(_steady_rise())) is None

    def test_still_falling_stays_flat(self):
        # Gate 2: a sharp drop but the recent leg is still sliding down -> the
        # market is not yet recovering.
        strat = OpenRebound()
        assert strat.evaluate("E", _buffer(_still_falling())) is None


class TestStructuralNone:
    def test_too_little_history(self):
        strat = OpenRebound()
        short = _buffer(_steady_rise(n=strat.warmup - 1))
        assert strat.evaluate("E", short) is None

    def test_non_positive_bid(self):
        strat = OpenRebound()
        closes = _rebound()
        closes[-1] = 0.0  # non-positive latest bid
        assert strat.evaluate("E", _buffer(closes)) is None

    def test_no_volatility_returns_none(self):
        # A perfectly constant curve has ATR == 0 (no true range): structurally
        # unscorable — the close profile could not size a stop.
        strat = OpenRebound()
        buf = _buffer([8000.0] * 120, pad=0.0)
        assert strat.evaluate("E", buf) is None


class TestScoreFloor:
    def test_below_floor_stays_flat(self):
        # A high floor rejects the same setup that scores without it, proving the
        # floor (not a structural reject) is what held it flat.
        floored = OpenRebound(min_score=0.99)
        assert floored.evaluate("E", _buffer(_rebound())) is None
        openfloor = OpenRebound(min_score=0.0)
        assert openfloor.evaluate("E", _buffer(_rebound())) is not None


class TestTent:
    """The sweet-spot response used to score the recovery fraction."""

    def test_ends_are_zero(self):
        assert _tent(0.0, 0.4) == 0.0
        assert _tent(1.0, 0.4) == 0.0
        assert _tent(-0.1, 0.4) == 0.0
        assert _tent(1.5, 0.4) == 0.0

    def test_peaks_at_the_ideal(self):
        assert _tent(0.4, 0.4) == 1.0

    def test_monotone_up_then_down(self):
        assert _tent(0.2, 0.4) < _tent(0.4, 0.4)
        assert _tent(0.7, 0.4) < _tent(0.4, 0.4)

    def test_degenerate_peak_is_safe(self):
        assert _tent(0.5, 0.0) == 0.0
        assert _tent(0.5, 1.0) == 0.0
