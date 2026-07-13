"""Tests for the robust cross-epic ranker (src/entry/open_saferanking.py).

Like ``open_ranking`` this entry is a *ranker*: ``evaluate`` returns a comparable
composite score for every scorable epic (the scheduler does the cross-epic
selection). These tests cover the registry, the score contract ([0, 1],
BUY-only), the structural ``None`` cases, the ``min_models_agree`` gate, the
``min_score`` floor, and — the point of this ranker — the *conjunctive* ranking:
that a clean, low-drawdown up-trend outscores both chop and a rise scarred by a
deep retracement, and that no single strong dimension can rescue a fragile
market the way the additive ``open_ranking`` sum would.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from src.entry import EntryIntent, OpenSafeRanking, get_entry_strategy
from src.entry.base import EntryStrategy
from src.entry.open_saferanking import _pullback_safety, _recent_bearish_factor
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


def _trending_up(n: int = 90, start: float = 8000.0, step: float = 1.0) -> list[float]:
    return [start + i * step for i in range(n)]


def _choppy(n: int = 90, start: float = 8000.0, amp: float = 5.0) -> list[float]:
    # Alternating up/down with no net drift: low ER, ~flat slope, weak projection.
    return [start + (amp if i % 2 else 0.0) for i in range(n)]


def _up_then_recent_drop(
    n: int = 90, start: float = 8000.0, step: float = 1.0, recent: int = 10
) -> list[float]:
    # Clean climb for the bulk of the window, then a sharp slide over the final
    # ``recent`` candles — the exact "market already rolling over at open" shape
    # the pre-open bearish malus targets.
    rise = [start + i * step for i in range(n - recent)]
    peak = rise[-1]
    drop = [peak - (j + 1) * step * 3 for j in range(recent)]
    return rise + drop


def _up_with_deep_pullback(n: int = 90, start: float = 8000.0) -> list[float]:
    # Same net rise as ``_trending_up`` but with a deep retracement mid-way: rises,
    # gives back most of the gain, then recovers. Net slope up, but a holder would
    # have suffered a large adverse excursion — the "unsafe" rise.
    closes: list[float] = []
    for i in range(n):
        if i < n // 3:
            closes.append(start + i)  # climb
        elif i < 2 * n // 3:
            closes.append(start + (n // 3) - (i - n // 3))  # deep give-back
        else:
            closes.append(start + (i - 2 * n // 3) + 1)  # recover
    return closes


class TestRegistry:
    def test_known_name_resolves(self):
        strat = get_entry_strategy("open_saferanking", _settings())
        assert isinstance(strat, OpenSafeRanking)

    def test_is_cross_epic_selection(self):
        assert OpenSafeRanking.cross_epic_selection is True

    def test_is_entry_strategy_instance(self):
        assert isinstance(OpenSafeRanking(), EntryStrategy)

    def test_rolling_selection_constants_on_class(self):
        strat = OpenSafeRanking()
        assert strat.concurrent_positions == 1
        assert strat.open_after_minutes == 60
        assert strat.wallet_reserve == 0.10

    def test_is_wallet_bounded(self):
        # This ranker opens epics until the wallet runs dry rather than holding a
        # single rolling position — the scheduler keys off this flag.
        assert OpenSafeRanking.wallet_bounded is True

    def test_weights_sum_to_one(self):
        strat = OpenSafeRanking()
        total = (
            strat.weight_projection
            + strat.weight_shape
            + strat.weight_safety
            + strat.weight_momentum
            + strat.weight_regime
            + strat.weight_spread
        )
        assert abs(total - 1.0) < 1e-9


class TestPullbackSafety:
    def test_monotone_climb_scores_high(self):
        assert _pullback_safety(_trending_up()) > 0.95

    def test_deep_pullback_scores_low(self):
        assert _pullback_safety(_up_with_deep_pullback()) < 0.5

    def test_flat_curve_scores_zero(self):
        assert _pullback_safety([8000.0] * 20) == 0.0

    def test_degenerate_short_input_scores_zero(self):
        assert _pullback_safety([8000.0]) == 0.0


class TestRecentBearishFactor:
    def test_rising_window_has_no_malus(self):
        assert _recent_bearish_factor([100.0 + i for i in range(10)], 0.003, 0.05) == 1.0

    def test_flat_window_has_no_malus(self):
        assert _recent_bearish_factor([100.0] * 10, 0.003, 0.05) == 1.0

    def test_clean_steep_drop_hits_the_floor(self):
        # A clean, steep slide (~0.5% decline over the window, R²≈1) earns close to
        # the full malus, dragging the factor down toward the floor.
        drop = [100.0 - i * 0.05 for i in range(10)]  # -0.45% over the window
        factor = _recent_bearish_factor(drop, 0.003, 0.05)
        assert factor < 0.2

    def test_shallow_drop_barely_dents(self):
        # A tiny decline well under the full-malus threshold keeps the factor high.
        drop = [100.0 - i * 0.0005 for i in range(10)]  # ~-0.0045% over the window
        factor = _recent_bearish_factor(drop, 0.003, 0.05)
        assert factor > 0.9

    def test_degenerate_short_input_has_no_malus(self):
        assert _recent_bearish_factor([100.0], 0.003, 0.05) == 1.0

    def test_factor_stays_within_bounds(self):
        drop = [100.0 - i * 0.05 for i in range(10)]
        assert 0.05 <= _recent_bearish_factor(drop, 0.003, 0.05) <= 1.0


class TestEvaluate:
    def test_rising_curve_emits_buy_with_bounded_score(self):
        intent = OpenSafeRanking().evaluate("TEST.EPIC", _buffer(_trending_up()))
        assert isinstance(intent, EntryIntent)
        assert intent.direction == "BUY"
        assert 0.0 <= intent.score <= 1.0
        assert intent.score > 0.5

    def test_insufficient_warmup_returns_none(self):
        # warmup = max(60, 30, 60, 10, 30, 60, 14) + 1 = 61; 40 candles is too few.
        assert (
            OpenSafeRanking().evaluate("TEST.EPIC", _buffer(_trending_up(40)))
            is None
        )

    def test_zero_volatility_returns_none(self):
        buf = EpicBuffer(epic="TEST.EPIC", max_candles=100)
        start = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)
        for i in range(90):
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
        assert OpenSafeRanking().evaluate("TEST.EPIC", buf) is None

    def test_min_models_agree_gate_blocks_non_rising(self):
        # Chop: essentially no model projects up -> structural reject.
        strat = OpenSafeRanking(min_models_agree=3)
        assert strat.evaluate("TEST.EPIC", _buffer(_choppy())) is None

    def test_min_score_floor_blocks_weak_setup(self):
        strat = OpenSafeRanking(min_score=1.01)
        assert strat.evaluate("TEST.EPIC", _buffer(_trending_up())) is None

    def test_uptrend_outranks_chop(self):
        strat = OpenSafeRanking(min_models_agree=0)
        up = strat.evaluate("TEST.EPIC", _buffer(_trending_up()))
        chop = strat.evaluate("TEST.EPIC", _buffer(_choppy()))
        assert up is not None and chop is not None
        assert up.score > chop.score

    def test_recent_drop_is_penalised_vs_clean_rise(self):
        # The pre-open safety: a market that climbed all window but is sliding down
        # over the last 10 minutes must score well below a still-clean rise.
        strat = OpenSafeRanking(min_models_agree=0)
        clean = strat.evaluate("TEST.EPIC", _buffer(_trending_up()))
        rolling_over = strat.evaluate("TEST.EPIC", _buffer(_up_then_recent_drop()))
        assert clean is not None and rolling_over is not None
        assert rolling_over.score < clean.score

    def test_recent_drop_malus_can_be_disabled(self):
        # With the full-malus threshold at 0 the guard is a no-op: the recent drop
        # no longer changes the score relative to the same curve scored normally.
        guarded = OpenSafeRanking(min_models_agree=0)
        disabled = OpenSafeRanking(min_models_agree=0, recent_drop_full_malus=0.0)
        curve = _up_then_recent_drop()
        with_malus = guarded.evaluate("TEST.EPIC", _buffer(curve))
        without = disabled.evaluate("TEST.EPIC", _buffer(curve))
        assert with_malus is not None and without is not None
        assert with_malus.score < without.score

    def test_clean_rise_outranks_rise_with_deep_pullback(self):
        # The robustness contract: a clean climb must outrank a rise that gave back
        # most of its gain, even though both net up over the window.
        strat = OpenSafeRanking(min_models_agree=0)
        clean = strat.evaluate("TEST.EPIC", _buffer(_trending_up()))
        scarred = strat.evaluate("TEST.EPIC", _buffer(_up_with_deep_pullback()))
        assert clean is not None and scarred is not None
        assert clean.score > scarred.score
