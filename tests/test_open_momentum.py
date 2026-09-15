"""Tests for the momentum-start ranker (src/entry/open_momentum.py).

``open_momentum`` opens only where two independent readings of the curve agree:
the *regime* says the market is going somewhere (``TREND_UP``) and the *move
phase* says the 9/21 leg carrying it has just begun (``START``, pointing up).
These tests cover the registry and selection contract, each of the two readings
refusing on its own, the direction check that separates a fresh down leg from a
fresh up one, the score used to order qualifying candidates, and the warm-up.

The book-side rule (``require_range_book`` — open nothing while an open position
is still riding its move) is enforced by the scheduler, not by ``evaluate``; it is
tested in ``tests/test_scheduler.py::TestRequireRangeBook``.
"""

import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from src.core.reading import CurveReading, MovePhase, Regime, Trend
from src.entry import OpenMomentum, get_entry_strategy
from src.entry.base import EntryStrategy
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


def _fresh_up_leg(
    drift: int = 52, rise: int = 8, start: float = 8000.0, step: float = 3.0
) -> list[float]:
    """A slow slide, then a young rise — the setup this entry exists for.

    ``rise = 8`` puts the 9/21 crossing 5 candles back: recent enough to still
    read ``START``, and steep enough for the 30-minute regime to read ``TREND_UP``.
    """
    slide = [start - 0.2 * i for i in range(drift)]
    return slide + [slide[-1] + step * (i + 1) for i in range(rise)]


def _established_climb(n: int = 70, start: float = 8000.0) -> list[float]:
    """A straight rise that has been going on for an hour — up, but not young."""
    return [start + i for i in range(n)]


def _chop(n: int = 70, start: float = 8000.0, amp: float = 8.0) -> list[float]:
    """A contained oscillation: fresh crossings all day, none of them a leg."""
    return [start + amp * math.sin(2 * math.pi * i / 12) for i in range(n)]


class TestRegistryAndContract:
    def test_known_name_resolves(self):
        strategy = get_entry_strategy("open_momentum", _settings())
        assert isinstance(strategy, OpenMomentum)

    def test_is_an_entry_strategy(self):
        assert isinstance(OpenMomentum(), EntryStrategy)

    def test_is_a_long_only_cross_epic_ranker(self):
        strategy = OpenMomentum()
        assert strategy.cross_epic_selection is True
        assert strategy.emits_shorts is False

    def test_declares_the_range_book_brake(self):
        """The "one bet at a time" rule is a book rule, enforced by the scheduler."""
        strategy = OpenMomentum()
        assert strategy.require_range_book is True
        # The brake and the entry's own regime gate read the same window.
        assert strategy.book_regime_window == strategy.context_minutes

    def test_opens_one_position_per_pass(self):
        """No count target — the book grows one position at a time, spaced out."""
        strategy = OpenMomentum()
        assert strategy.wallet_bounded is True
        assert strategy.open_cooldown_minutes >= 1

    def test_no_participation_gate(self):
        """Every gate is absolute, so a thin universe is no reason to refuse."""
        strategy = OpenMomentum()
        assert strategy.min_participation_ratio == 0.0
        assert strategy.min_participation_count == 0


class TestTheSetupItOpens:
    def test_fresh_up_leg_in_an_up_regime_opens(self):
        buf = _buffer(_fresh_up_leg())
        # The premise, stated rather than assumed: both readings agree here.
        reading = CurveReading.of(buf, regime_window=30)
        assert reading.regime is Regime.TREND_UP
        assert reading.move_phase is MovePhase.START
        assert reading.move_direction is Trend.UP

        intent = OpenMomentum().evaluate("TEST.EPIC", buf)

        assert intent is not None
        assert intent.direction == "BUY"

    def test_score_is_the_context_efficiency_ratio(self):
        buf = _buffer(_fresh_up_leg())
        expected = CurveReading.of(buf, regime_window=30).amplitude(30).efficiency

        intent = OpenMomentum().evaluate("TEST.EPIC", buf)

        assert intent is not None
        assert intent.score == expected

    def test_the_straighter_path_ranks_higher(self):
        """The score only orders candidates that have already qualified."""
        strategy = OpenMomentum()
        straight = strategy.evaluate("TEST.EPIC", _buffer(_fresh_up_leg()))
        # Same young rise, reached through a noisier approach: same setup, worse
        # path, so it must rank below.
        noisy_closes = _fresh_up_leg()
        for i in range(0, 52, 2):
            noisy_closes[i] += 1.0
        noisy = strategy.evaluate("TEST.EPIC", _buffer(noisy_closes))

        assert straight is not None and noisy is not None
        assert straight.score > noisy.score


class TestWhatItRefuses:
    def test_an_established_rise_is_too_late(self):
        """``TREND_UP`` alone is not enough — this one has been running for an hour."""
        buf = _buffer(_established_climb())
        assert CurveReading.of(buf, regime_window=30).regime is Regime.TREND_UP
        assert CurveReading.of(buf, regime_window=30).move_phase is not MovePhase.START

        assert OpenMomentum().evaluate("TEST.EPIC", buf) is None

    def test_a_fresh_cross_inside_chop_is_refused(self):
        """``START`` alone is not enough — the regime is what rejects the sawing."""
        buf = _buffer(_chop())
        reading = CurveReading.of(buf, regime_window=30)
        assert reading.regime is not Regime.TREND_UP

        assert OpenMomentum().evaluate("TEST.EPIC", buf) is None

    def test_a_fresh_down_leg_is_refused(self):
        """Long-only: the mirrored setup emits nothing rather than a SELL."""
        falling = [-close for close in _fresh_up_leg()]
        buf = _buffer([16000 + close for close in falling])
        assert CurveReading.of(buf, regime_window=30).move_direction is Trend.DOWN

        assert OpenMomentum().evaluate("TEST.EPIC", buf) is None

    def test_a_flat_market_is_refused(self):
        assert OpenMomentum().evaluate("TEST.EPIC", _buffer([8000.0] * 70)) is None


class TestWarmup:
    def test_warmup_is_the_candle_count(self):
        strategy = OpenMomentum()
        assert strategy.warmup == strategy.warmup_minutes == 60

    def test_a_short_buffer_scores_nothing(self):
        strategy = OpenMomentum()
        closes = _fresh_up_leg()
        buf = _buffer(closes[-(strategy.warmup - 1) :])

        assert strategy.evaluate("TEST.EPIC", buf) is None
