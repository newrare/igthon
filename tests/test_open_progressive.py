"""Tests for the single-position ranker with a risk ladder (open_progressive).

Two halves, matching the strategy's two ideas:

- the **ranking** — ``evaluate`` times how long a market needs to cover its own
  spread and how sure that reading is, so a fast clean climb outranks a slower or
  scarred one, and chop / a falling / a fading / an expensively-quoted market is
  vetoed outright rather than merely ranked lower;
- the **risk ladder** — ``risk_band`` walks up only on money already banked and
  straight back down on a give-back, and ``filter_by_risk`` keeps exactly the
  candidates priced into the unlocked band. The invariant test is the important
  one: whatever the day's P&L, a full stop-out can never put it below −50 €.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.entry import EntryIntent, OpenProgressive, get_entry_strategy
from src.entry.base import EntryStrategy
from src.feed.price_buffer import Candle, EpicBuffer


def _settings(**overrides) -> SimpleNamespace:
    # Every parameter is a class constant, so ``from_settings`` ignores settings
    # entirely; this stand-in only needs to exist for the registry call.
    return SimpleNamespace(**overrides)


def _buffer(
    closes: list[float],
    spread: float = 0.5,
    epic: str = "TEST.EPIC",
    spike: tuple[int, float] | None = None,
):
    """Build a buffer from closes; ``spike`` plants a lone wick high at an index.

    The wick is how a *resistance* is put overhead without deforming the curve:
    it leaves every close (and so every trend, regime and efficiency reading)
    untouched, so a test can move the headroom rule alone.
    """
    buf = EpicBuffer(epic=epic, max_candles=len(closes) + 10)
    start = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)
    prev = closes[0]
    for i, close in enumerate(closes):
        high = max(prev, close) + 0.1
        low = min(prev, close) - 0.1
        if spike is not None and i == spike[0]:
            high = spike[1]
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


def _ramp(step: float = 1.0, n: int = 90, start: float = 8000.0) -> list[float]:
    """A monotone climb — the shape the ranker is built to crown."""
    return [start + i * step for i in range(n)]


def _chop(n: int = 90, start: float = 8000.0, amp: float = 5.0) -> list[float]:
    """Alternating candles with no net drift: travels far, goes nowhere."""
    return [start + (amp if i % 2 else 0.0) for i in range(n)]


def _scarred(n: int = 90, start: float = 8000.0) -> list[float]:
    """A climb interrupted by a real give-back, then resumed."""
    rise = [start + i for i in range(60)]
    dip = [rise[-1] - j for j in range(6)]
    return rise + dip + [dip[-1] + j for j in range(n - len(rise) - len(dip))]


def _fading(n: int = 90, start: float = 8000.0) -> list[float]:
    """A long climb that has flattened over the last ten minutes."""
    rise = [start + i for i in range(n - 10)]
    return rise + [rise[-1]] * 10


class TestRegistryAndContract:
    def test_registered_under_its_name(self):
        strategy = get_entry_strategy("open_progressive", _settings())
        assert isinstance(strategy, OpenProgressive)
        assert isinstance(strategy, EntryStrategy)
        assert strategy.name == "open_progressive"

    def test_selection_knobs(self):
        strategy = OpenProgressive()
        # One market at a time, funded by the ladder rather than by the wallet.
        assert strategy.cross_epic_selection is True
        assert strategy.concurrent_positions == 1
        assert strategy.wallet_bounded is False
        assert strategy.uses_risk_ladder is True
        # Long-only: the ladder's invariant is written on a stop-out, and shorts
        # are not part of this entry's thesis.
        assert strategy.emits_shorts is False

    def test_a_ranking_needs_enough_candidates(self):
        strategy = OpenProgressive()
        # Both participation gates are on: this entry acts on a single winner, so a
        # tournament of three epics must not crown one.
        assert strategy.min_participation_ratio == 0.5
        assert strategy.min_participation_count == 20

    def test_weights_sum_to_one(self):
        strategy = OpenProgressive()
        total = (
            strategy.weight_speed
            + strategy.weight_quality
            + strategy.weight_efficiency
            + strategy.weight_pullback
            + strategy.weight_alignment
        )
        assert total == pytest.approx(1.0)

    def test_score_is_a_buy_in_the_unit_interval(self):
        intent = OpenProgressive().evaluate("A", _buffer(_ramp()))
        assert isinstance(intent, EntryIntent)
        assert intent.direction == "BUY"
        assert 0.0 < intent.score <= 1.0


class TestVetoes:
    def test_cold_buffer_is_not_scored(self):
        strategy = OpenProgressive()
        assert strategy.evaluate("A", _buffer(_ramp(n=strategy.warmup - 1))) is None

    def test_chop_is_refused(self):
        assert OpenProgressive().evaluate("A", _buffer(_chop())) is None

    def test_falling_market_is_refused(self):
        falling = [8000.0 - i for i in range(90)]
        assert OpenProgressive().evaluate("A", _buffer(falling)) is None

    def test_fading_trend_is_refused(self):
        # Still rising over the hour, but the last ten minutes have stalled — the
        # earliest readable warning that the move is over.
        assert OpenProgressive().evaluate("A", _buffer(_fading())) is None

    def test_spread_wider_than_a_candle_is_refused(self):
        # Same climb, but the round trip now costs more than the market moves in a
        # candle: break-even stops being a near-term event.
        assert OpenProgressive().evaluate("A", _buffer(_ramp(), spread=30.0)) is None

    def test_break_even_out_of_reach_is_refused(self):
        # A rise so slow that covering the spread takes longer than the ceiling.
        strategy = OpenProgressive()
        strategy.max_minutes_to_breakeven = 1.0
        assert strategy.evaluate("A", _buffer(_ramp(step=0.4))) is None

    def test_too_little_room_to_the_resistance_is_refused(self):
        # A lone wick 111 points overhead leaves a real resistance above the
        # entry without touching a single close, so the headroom rule is the only
        # thing that moves between the two calls.
        buf = _buffer(_ramp(), spike=(40, 8200.0))
        assert OpenProgressive().evaluate("A", buf) is not None
        strategy = OpenProgressive()
        strategy.min_room_atr = 200.0  # ~240 points of ATR demanded, only 111 free
        assert strategy.evaluate("A", buf) is None

    def test_score_floor_can_refuse_the_whole_pool(self):
        strategy = OpenProgressive()
        strategy.min_score = 0.99
        assert strategy.evaluate("A", _buffer(_ramp())) is None


class TestRanking:
    def test_faster_break_even_outranks_slower(self):
        strategy = OpenProgressive()
        fast = strategy.evaluate("FAST", _buffer(_ramp(step=1.0)))
        slow = strategy.evaluate("SLOW", _buffer(_ramp(step=0.4)))
        assert fast is not None and slow is not None
        # Same spread, same shape: the only difference is how long the bid needs
        # to climb one spread.
        assert fast.score > slow.score

    def test_clean_climb_outranks_a_scarred_one(self):
        strategy = OpenProgressive()
        clean = strategy.evaluate("CLEAN", _buffer(_ramp()))
        scarred = strategy.evaluate("SCARRED", _buffer(_scarred()))
        assert clean is not None and scarred is not None
        assert clean.score > scarred.score


class TestRiskBand:
    """The ladder rung, derived from the day's realised P&L on every pass."""

    @pytest.mark.parametrize(
        "realised, expected",
        [
            (0.0, (0.0, 50.0)),  # flat day: the base allowance only
            (-120.0, (0.0, 50.0)),  # a losing day never unlocks anything
            (49.99, (0.0, 50.0)),  # one cent short of the first rung
            (50.0, (50.0, 100.0)),  # exactly the first rung: step up
            (99.0, (50.0, 100.0)),
            (150.0, (100.0, 200.0)),
            (449.0, (100.0, 200.0)),  # 500 needs 450 banked (base 50 + 450)
            (450.0, (200.0, 500.0)),
            (10_000.0, (200.0, 500.0)),  # the top rung is the ceiling
        ],
    )
    def test_band_for_realised_pnl(self, realised, expected):
        assert OpenProgressive().risk_band(realised) == expected

    def test_a_give_back_walks_the_ladder_down(self):
        strategy = OpenProgressive()
        assert strategy.risk_band(180.0) == (100.0, 200.0)
        # The gains are handed back: the band is recomputed, not latched.
        assert strategy.risk_band(20.0) == (0.0, 50.0)

    def test_a_full_stop_out_never_puts_the_day_below_the_base_rung(self):
        """The whole point of the ladder — escalate only on the market's money."""
        strategy = OpenProgressive()
        base = strategy.risk_ladder[0]
        for realised in (0, 1, 49, 50, 120, 200, 449, 450, 900, 5000):
            _, ceiling = strategy.risk_band(float(realised))
            assert realised - ceiling >= -base


class TestFilterByRisk:
    def _ranked(self, epics: list[str]) -> list[tuple[EntryIntent, EpicBuffer]]:
        return [
            (
                EntryIntent(epic=e, direction="BUY", score=1.0 - i / 100),
                _buffer(_ramp()),
            )
            for i, e in enumerate(epics)
        ]

    def test_keeps_only_the_unlocked_band(self):
        strategy = OpenProgressive()
        ranked = self._ranked(["CHEAP", "MID", "RICH"])
        risks = {"CHEAP": 20.0, "MID": 70.0, "RICH": 300.0}
        # +60 € banked unlocks the 50-100 € rung: the cheap market is now beneath
        # the strategy and the rich one is still out of reach.
        kept = strategy.filter_by_risk(ranked, risks=risks, realised_today=60.0)
        assert [intent.epic for intent, _ in kept] == ["MID"]

    def test_flat_day_takes_the_small_markets_only(self):
        strategy = OpenProgressive()
        ranked = self._ranked(["CHEAP", "MID"])
        kept = strategy.filter_by_risk(
            ranked, risks={"CHEAP": 20.0, "MID": 70.0}, realised_today=0.0
        )
        assert [intent.epic for intent, _ in kept] == ["CHEAP"]

    def test_unknown_risk_is_dropped_not_assumed_cheap(self):
        strategy = OpenProgressive()
        ranked = self._ranked(["UNKNOWN", "KNOWN"])
        kept = strategy.filter_by_risk(
            ranked, risks={"UNKNOWN": None, "KNOWN": 30.0}, realised_today=0.0
        )
        assert [intent.epic for intent, _ in kept] == ["KNOWN"]

    def test_missing_epic_is_dropped_too(self):
        strategy = OpenProgressive()
        kept = strategy.filter_by_risk(
            self._ranked(["ABSENT"]), risks={}, realised_today=0.0
        )
        assert kept == []

    def test_ranking_order_is_preserved(self):
        strategy = OpenProgressive()
        ranked = self._ranked(["BEST", "SECOND", "THIRD"])
        risks = {"BEST": 10.0, "SECOND": 20.0, "THIRD": 30.0}
        kept = strategy.filter_by_risk(ranked, risks=risks, realised_today=0.0)
        assert [intent.epic for intent, _ in kept] == ["BEST", "SECOND", "THIRD"]

    def test_falls_back_down_when_the_band_is_empty(self):
        strategy = OpenProgressive()
        ranked = self._ranked(["CHEAP", "RICH"])
        # +60 € banked opens the 50-100 € rung, but nothing is priced there: rather
        # than sit out the afternoon the selector trades DOWN to the small markets.
        kept = strategy.filter_by_risk(
            ranked, risks={"CHEAP": 20.0, "RICH": 300.0}, realised_today=60.0
        )
        assert [intent.epic for intent, _ in kept] == ["CHEAP"]

    def test_stretches_the_ceiling_when_nothing_is_affordable_below(self):
        strategy = OpenProgressive()
        # Flat day: the rung is 0-50 €, and every market is dearer. 55 € is within
        # the 25 % stretch (62.50 €); 90 € is not.
        kept = strategy.filter_by_risk(
            self._ranked(["JUST_ABOVE", "FAR_ABOVE"]),
            risks={"JUST_ABOVE": 55.0, "FAR_ABOVE": 90.0},
            realised_today=0.0,
        )
        assert [intent.epic for intent, _ in kept] == ["JUST_ABOVE"]

    def test_the_cheaper_fallback_is_preferred_over_the_stretch(self):
        strategy = OpenProgressive()
        # The 50-100 € band is empty; a 20 € market and a 110 € one are available.
        # Trading down keeps the ladder's invariant, stretching does not — so the
        # cheap one wins and the dear one is never reached.
        kept = strategy.filter_by_risk(
            self._ranked(["CHEAP", "OVER"]),
            risks={"CHEAP": 20.0, "OVER": 110.0},
            realised_today=60.0,
        )
        assert [intent.epic for intent, _ in kept] == ["CHEAP"]

    def test_stays_flat_when_every_scope_is_empty(self):
        strategy = OpenProgressive()
        kept = strategy.filter_by_risk(
            self._ranked(["FAR_ABOVE"]),
            risks={"FAR_ABOVE": 5_000.0},
            realised_today=0.0,
        )
        assert kept == []

    def test_fallbacks_can_be_disabled(self):
        strategy = OpenProgressive()
        strategy.allow_cheaper_fallback = False
        strategy.band_stretch = 0.0
        kept = strategy.filter_by_risk(
            self._ranked(["CHEAP", "OVER"]),
            risks={"CHEAP": 20.0, "OVER": 110.0},
            realised_today=60.0,
        )
        assert kept == []

    def test_non_strict_band_keeps_the_cheaper_markets(self):
        strategy = OpenProgressive()
        strategy.strict_band = False
        ranked = self._ranked(["CHEAP", "MID", "RICH"])
        risks = {"CHEAP": 20.0, "MID": 70.0, "RICH": 300.0}
        kept = strategy.filter_by_risk(ranked, risks=risks, realised_today=60.0)
        # The band becomes a plain ceiling: only what is unaffordable is dropped.
        assert [intent.epic for intent, _ in kept] == ["CHEAP", "MID"]
