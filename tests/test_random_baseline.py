"""Tests for the random-direction baseline (src/backtest/random_baseline.py)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.backtest.random_baseline import (
    BaselineReport,
    DirectionShuffle,
    coin_flip,
    empirical_p_value,
    mean_r,
    normal_p_value,
    trade_r,
)
from src.entry.base import EntryIntent
from src.feed.price_buffer import Candle, EpicBuffer


def _buffer(epic: str = "X", minutes: int = 3) -> EpicBuffer:
    buf = EpicBuffer(epic=epic, max_candles=50)
    t0 = datetime(2026, 7, 7, 10, 0, tzinfo=UTC)
    for i in range(minutes):
        buf.add(
            Candle(
                timestamp=t0 + timedelta(minutes=i),
                bid_open=100,
                bid_close=100,
                bid_high=101,
                bid_low=99,
                offer_open=100.1,
                offer_close=100.1,
                offer_high=101.1,
                offer_low=99.1,
            )
        )
    return buf


class _AlwaysBuy:
    """Minimal long-only ranker stand-in with a few scheduler knobs."""

    name = "always_buy"
    cross_epic_selection = True
    emits_shorts = False
    open_cooldown_minutes = 5
    warmup = 2

    def evaluate(self, epic, buf):
        return EntryIntent(epic=epic, direction="BUY", score=0.7)

    def filter_ranked(self, ranked):
        return ranked[:1]


class TestCoinFlip:
    def test_is_deterministic(self):
        assert coin_flip(3, "CC", "2026-07-07T10:00") == coin_flip(
            3, "CC", "2026-07-07T10:00"
        )

    def test_draws_both_sides_about_evenly(self):
        sides = [coin_flip(0, "CC", f"t{i}") for i in range(2000)]
        assert 0.45 < sides.count("BUY") / len(sides) < 0.55

    def test_seeds_are_independent(self):
        a = [coin_flip(1, "CC", f"t{i}") for i in range(500)]
        b = [coin_flip(2, "CC", f"t{i}") for i in range(500)]
        agreement = sum(x == y for x, y in zip(a, b)) / len(a)
        assert 0.4 < agreement < 0.6


class TestDirectionShuffle:
    def test_keeps_the_open_and_its_score_but_flips_the_side(self):
        wrapper = DirectionShuffle(_AlwaysBuy(), seed=0)
        sides = set()
        for minutes in range(3, 40):
            intent = wrapper.evaluate("X", _buffer(minutes=minutes))
            assert intent.epic == "X" and intent.score == 0.7
            sides.add(intent.direction)
        assert sides == {"BUY", "SELL"}

    def test_same_moment_same_side(self):
        wrapper = DirectionShuffle(_AlwaysBuy(), seed=5)
        assert (
            wrapper.evaluate("X", _buffer()).direction
            == wrapper.evaluate("X", _buffer()).direction
        )

    def test_no_intent_stays_no_intent(self):
        inner = _AlwaysBuy()
        inner.evaluate = lambda epic, buf: None
        assert DirectionShuffle(inner, seed=0).evaluate("X", _buffer()) is None

    def test_scheduler_knobs_are_the_inner_strategy_s(self):
        wrapper = DirectionShuffle(_AlwaysBuy(), seed=0)
        assert wrapper.cross_epic_selection is True
        assert wrapper.open_cooldown_minutes == 5
        assert wrapper.warmup == 2
        assert wrapper.filter_ranked([1, 2]) == [1]

    def test_always_emits_shorts(self):
        # A coin flip draws SELL half the time, even for a long-only strategy.
        assert DirectionShuffle(_AlwaysBuy(), seed=0).emits_shorts is True
        assert DirectionShuffle(_AlwaysBuy(), seed=4).name == "always_buy~random4"


def _trade(direction="BUY", open_=100.0, close=103.0, risk=2.0):
    return SimpleNamespace(
        direction=direction,
        level_open=open_,
        level_close=close,
        euro_stop=risk,
        epic="X",
    )


class TestTradeR:
    def test_long_and_short(self):
        assert trade_r(_trade("BUY", 100, 103, 2)) == pytest.approx(1.5)
        assert trade_r(_trade("SELL", 100, 103, 2)) == pytest.approx(-1.5)

    def test_unexpressible_risk_is_none(self):
        assert trade_r(_trade(risk=0.0)) is None
        assert trade_r(_trade(close=None)) is None

    def test_mean_skips_unexpressible_trades(self):
        trades = [_trade(close=104), _trade(risk=0.0), _trade(close=98)]
        assert mean_r(trades) == pytest.approx(0.5)
        assert mean_r([]) == 0.0


class TestPValues:
    def test_empirical_counts_ties_and_the_real_run(self):
        assert empirical_p_value(0.5, [0.1, 0.5, 0.9]) == pytest.approx(3 / 4)
        assert empirical_p_value(2.0, [0.1, 0.2]) == pytest.approx(1 / 3)

    def test_normal_z_and_p(self):
        z, p = normal_p_value(2.0, [-1.0, 0.0, 1.0])
        assert z == pytest.approx(2.0)
        assert p == pytest.approx(0.02275, abs=1e-4)

    def test_normal_needs_a_spread(self):
        z, p = normal_p_value(1.0, [0.5, 0.5])
        assert z != z and p != p  # NaN: a constant null proves nothing

    def test_report_summary(self):
        report = BaselineReport(
            strategy="s",
            trades=100,
            mean_r=0.2,
            euro=50.0,
            null_trades=[98, 101, 99],
            null_mean_r=[-0.1, 0.0, 0.1],
            null_euro=[-10.0, 0.0, 10.0],
        )
        summary = report.summary()
        assert summary["seeds"] == 3
        assert summary["p_value_r"] == pytest.approx(1 / 4)
        assert summary["z_r"] == pytest.approx(2.0)
        assert summary["null_trades_median"] == 99
