"""Tests for the steady-curve ranker (src/entry/open_steady.py).

Like its siblings ``open_linear`` / ``open_slope`` this entry is a *ranker*:
``evaluate`` returns a comparable score for every scorable epic and the scheduler
does the cross-epic selection. These tests cover the registry, the two-sided
contract, the score's discrimination between the four curve shapes the spec names
(regular line / zig-zag / spike / flat), the hard gates (contiguity, spike veto,
flat slope, structural ``None``) and the selection-layer constants.

The selection-layer knobs (``block_open_while_alive``,
``min_participation_count``) are asserted here as the strategy's contract; they
are exercised against the scheduler in ``tests/test_scheduler.py``.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from src.entry import OpenSteady, get_entry_strategy
from src.entry.base import EntryStrategy
from src.entry.open_steady import _step_concentration
from src.feed.price_buffer import Candle, EpicBuffer


def _settings(**overrides) -> SimpleNamespace:
    # The ranker's parameters are class constants, so ``from_settings`` ignores
    # settings entirely; this stand-in only needs to exist for the registry call.
    return SimpleNamespace(**overrides)


def _buffer(
    closes: list[float],
    spread: float = 0.5,
    pad: float = 0.1,
    gap_at: int | None = None,
) -> EpicBuffer:
    """Build a buffer from bid closes, one candle per minute.

    ``gap_at`` inserts a 10-minute hole before that index, simulating a stalled
    subscription (the contiguity gate's target).
    """
    buf = EpicBuffer(epic="TEST.EPIC", max_candles=len(closes) + 10)
    stamp = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)
    prev = closes[0]
    for i, close in enumerate(closes):
        if gap_at is not None and i == gap_at:
            stamp += timedelta(minutes=10)
        high = max(prev, close) + pad
        low = min(prev, close) - pad
        buf.add(
            Candle(
                timestamp=stamp,
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
        stamp += timedelta(minutes=1)
    return buf


def _line(n: int = 40, start: float = 8000.0, step: float = 2.0) -> list[float]:
    """A perfectly regular climb — the shape the ranker is built to crown."""
    return [start + i * step for i in range(n)]


def _zigzag(n: int = 40, start: float = 8000.0, amplitude: float = 8.0) -> list[float]:
    """Up-and-down all the time, with a small net drift so the slope is non-zero."""
    return [start + i * 0.2 + (amplitude if i % 2 else -amplitude) for i in range(n)]


def _spike(n: int = 40, start: float = 8000.0, jump: float = 40.0) -> list[float]:
    """Flat, then one candle carrying the whole move, then flat again."""
    closes = [start] * n
    for i in range(n - 4, n):
        closes[i] = start + jump
    return closes


def _flat(n: int = 40, start: float = 8000.0) -> list[float]:
    """Straight and direct but going nowhere — the anti-flat term's target."""
    return [start + i * 0.001 for i in range(n)]


# --- registry / contract ----------------------------------------------------


def test_registered_and_built_from_settings():
    strategy = get_entry_strategy("open_steady", _settings())
    assert isinstance(strategy, OpenSteady)
    assert isinstance(strategy, EntryStrategy)
    assert strategy.name == "open_steady"


def test_selection_layer_contract():
    strategy = OpenSteady()
    assert strategy.cross_epic_selection is True
    assert strategy.emits_shorts is True  # BUY and SELL
    assert strategy.wallet_bounded is True
    assert strategy.open_cooldown_minutes == 5
    # A ranking is valid only above 20 candidate epics: strictly more than 20.
    assert strategy.min_participation_count == 21
    # The count carries the rule, so the ratio gate is deliberately disabled.
    assert strategy.min_participation_ratio == 0.0
    assert strategy.block_open_while_alive is True
    # The spec's 30 consecutive readings.
    assert strategy.warmup == 30
    # Weights are a partition of 1.0, so the geometric mean stays in [0, 1].
    total = (
        strategy.weight_linearity
        + strategy.weight_directness
        + strategy.weight_smoothness
        + strategy.weight_visibility
    )
    assert abs(total - 1.0) < 1e-9


# --- direction --------------------------------------------------------------


def test_clean_rise_is_bought():
    intent = OpenSteady().evaluate("TEST.EPIC", _buffer(_line()))
    assert intent is not None
    assert intent.direction == "BUY"
    assert 0.0 <= intent.score <= 1.0


def test_clean_fall_is_sold():
    intent = OpenSteady().evaluate("TEST.EPIC", _buffer(_line(step=-2.0)))
    assert intent is not None
    assert intent.direction == "SELL"


def test_rise_and_fall_score_almost_identically():
    """A mirrored curve ranks on the same scale, to within the price denominator.

    Three of the four components are exactly sign-free (R², the Kaufman ER and the
    step concentration all read magnitudes). ``visibility`` divides the net move by
    the *current* bid, and a mirrored pair does not end at the same price — a
    78-point climb from 8000 ends at 8078 while the fall ends at 7922 — so the same
    absolute move is a slightly different *relative* one. The residual asymmetry is
    a fraction of a percent, far below any ranking decision, but it is real and not
    worth pretending away.
    """
    up = OpenSteady().evaluate("TEST.EPIC", _buffer(_line(step=2.0)))
    down = OpenSteady().evaluate("TEST.EPIC", _buffer(_line(step=-2.0)))
    assert up is not None and down is not None
    assert abs(up.score - down.score) < 0.01


# --- the four shapes the spec names -----------------------------------------


def test_regular_line_outranks_a_faster_but_erratic_curve():
    """ "Propre et nette" beats "rapide": the clean line wins on score."""
    strategy = OpenSteady()
    clean = strategy.evaluate("TEST.EPIC", _buffer(_line(step=2.0)))
    erratic = strategy.evaluate("TEST.EPIC", _buffer(_zigzag(amplitude=8.0)))
    assert clean is not None
    # The zig-zag either fails the floor outright or ranks below the clean line.
    assert erratic is None or erratic.score < clean.score


def test_strong_movers_are_never_tied():
    """Regression guard: the ranker must produce a TOTAL order.

    A hard ``min(m / target, 1)`` clamp on the magnitude term made every clean
    curve past ``move_target`` score exactly 1.000, so "keep only the best" fell
    back to whatever order the epics arrived in. The soft saturation keeps the
    order strict while still flattening the returns on speed.
    """
    strategy = OpenSteady()
    scores = [
        strategy.evaluate("TEST.EPIC", _buffer(_line(step=step))).score
        for step in (2.0, 8.0, 32.0)
    ]
    assert scores == sorted(scores)
    assert len(set(scores)) == 3  # strictly ordered, no ties
    assert all(score < 1.0 for score in scores)
    # Diminishing returns: 4x the speed must buy far less than the first step did.
    assert (scores[2] - scores[1]) < (scores[1] - scores[0])


def test_a_clean_moderate_curve_outranks_a_fast_but_rougher_one():
    """The spec's core preference, asserted on the composite score."""
    strategy = OpenSteady()
    clean = strategy.evaluate("TEST.EPIC", _buffer(_line(step=1.0)))
    # Fast overall, but the path wobbles: same net travel reached unevenly.
    rough = strategy.evaluate(
        "TEST.EPIC",
        _buffer([8000.0 + i * 4.0 + (6.0 if i % 3 else -6.0) for i in range(40)]),
    )
    assert clean is not None
    assert rough is None or clean.score > rough.score


def test_zigzag_is_rejected():
    """A curve that goes up and down all the time must not be crowned."""
    assert OpenSteady().evaluate("TEST.EPIC", _buffer(_zigzag())) is None


def test_spike_is_vetoed():
    """One candle carrying the whole move is a spike, not a trend."""
    assert OpenSteady().evaluate("TEST.EPIC", _buffer(_spike())) is None


def test_flat_curve_is_rejected_despite_perfect_regularity():
    """Straight, direct and smooth — but going nowhere, so not tradable."""
    intent = OpenSteady().evaluate("TEST.EPIC", _buffer(_flat()))
    assert intent is None


def test_spike_survives_the_efficiency_ratio_but_not_smoothness():
    """Regression guard on the module's central claim.

    A lone jump scores ER = 1.0 (its maximum), so ``directness`` cannot reject it
    — only the step-concentration term can. If the spike veto is ever removed,
    this documents what breaks.
    """
    from src.core.indicators import efficiency_ratio

    window = _buffer(_spike()).bid_closes[-10:]
    assert efficiency_ratio(window, len(window) - 1) == 1.0  # ER is fooled
    assert _step_concentration(window) > OpenSteady().max_step_share  # this is not


# --- step concentration -----------------------------------------------------


def test_step_concentration_of_a_regular_line_is_its_structural_minimum():
    values = [0.0, 1.0, 2.0, 3.0, 4.0]  # 4 equal steps
    assert abs(_step_concentration(values) - 1.0 / 4) < 1e-9


def test_step_concentration_of_a_single_jump_is_one():
    values = [10.0, 10.0, 10.0, 20.0]
    assert _step_concentration(values) == 1.0


def test_step_concentration_of_a_motionless_window_reads_as_not_a_trend():
    assert _step_concentration([5.0, 5.0, 5.0]) == 1.0


# --- hard gates -------------------------------------------------------------


def test_gap_in_the_readings_is_rejected():
    """30 readings are not enough — they must be consecutive."""
    strategy = OpenSteady()
    closes = _line()
    assert strategy.evaluate("TEST.EPIC", _buffer(closes)) is not None
    # Same curve, one 10-minute hole inside the scored window.
    assert strategy.evaluate("TEST.EPIC", _buffer(closes, gap_at=35)) is None


def test_too_few_readings_returns_none():
    strategy = OpenSteady()
    assert strategy.evaluate("TEST.EPIC", _buffer(_line(n=29))) is None
    assert strategy.evaluate("TEST.EPIC", _buffer(_line(n=30))) is not None


def test_empty_buffer_returns_none():
    assert OpenSteady().evaluate("TEST.EPIC", EpicBuffer(epic="TEST.EPIC")) is None


def test_zero_volatility_returns_none():
    """ATR ≤ 0 leaves the close profile unable to size a stop."""
    buf = _buffer(_flat(), pad=0.0)
    # A dead-flat curve with no intra-candle range has no true range at all.
    assert OpenSteady().evaluate("TEST.EPIC", buf) is None


def test_non_positive_bid_returns_none():
    assert OpenSteady().evaluate("TEST.EPIC", _buffer([0.0] * 40)) is None


# --- score floor ------------------------------------------------------------


def test_score_floor_can_be_lowered_to_rank_purely():
    """With the floor off, a mediocre curve is ranked instead of dropped."""
    strategy = OpenSteady(min_score=0.0)
    intent = strategy.evaluate("TEST.EPIC", _buffer(_zigzag()))
    assert intent is not None
    assert intent.score < 0.60  # still scored low — it is just not vetoed


def test_higher_floor_rejects_a_curve_the_default_accepts():
    buf = _buffer(_line(step=0.5))  # clean but modest travel
    assert OpenSteady(min_score=0.0).evaluate("TEST.EPIC", buf) is not None
    assert OpenSteady(min_score=0.99).evaluate("TEST.EPIC", buf) is None
