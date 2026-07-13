"""Cross-epic ranker — a *robust* sibling of ``open_ranking``.

Like :class:`~src.entry.open_ranking.OpenRanking` this is a **ranker**, not a
gate (``cross_epic_selection = True``): the scheduler scores every tradable
epic, ranks them and keeps a target number of rolling positions open all day,
re-ranking the moment a position closes. This module owns only the *per-epic*
half — "how promising/rising is this curve right now?" — and stays exit-agnostic
(:meth:`evaluate` emits an :class:`~src.entry.base.EntryIntent` carrying only the
BUY direction and a comparable opening score; the stop/target/trailing belong to
the composed :class:`~src.exit.base.CloseProfile`).

Why a second ranker — what "safe" changes
------------------------------------------

``open_ranking`` combines its components as a **weighted sum**::

    score = w_proj·projection + w_shape·shape + w_mom·momentum
            + w_regime·regime + w_spread·spread

That is *compensatory*: a single strong component (the projection carries 40% of
the weight) can rescue an otherwise weak market, so the crowned epic can be one
where only the theoretical projection is high and the trend is actually choppy,
flat or full of deep pull-backs — exactly the fragile pick. This ranker is built
so a market ranks high **only when every dimension of a clear, safe rise holds at
once**. Three deliberate robustness upgrades:

1. **Conjunctive scoring — a weighted geometric mean, not a sum.** The composite
   is ``Π componentᵢ ^ wᵢ`` (weights sum to 1, so it stays in [0, 1]). The
   geometric mean is bounded above by its smallest term, so one weak dimension
   collapses the whole score toward zero: no single strong component can
   compensate for a weak one. Each component is floored at a small ``epsilon`` so
   the composite stays strictly monotone (still rankable among mediocre markets —
   the goal is to hold the *least-bad* when forced) rather than snapping ties to
   a flat 0.
2. **Breadth-scaled projection.** Instead of the raw consensus score, the
   projection component is ``consensus.score × (agree / active)`` — the fraction
   of independent models actually projecting up. A lone over-confident model is
   discounted; genuine multi-model **unanimity** is rewarded. A hard structural
   gate (``min_models_agree``) refuses any market almost no model projects up.
3. **Two "safety" components ``open_ranking`` lacks:**
   - **Pull-back safety** — ``1 - max_drawdown / range``: the deepest
     peak-to-trough retracement of the bid curve over the window, relative to its
     total range. A monotone climb scores ~1; a rise punctuated by violent
     retracements scores low even when the net slope is up. This is the real
     adverse-excursion risk a would-be holder faced, which the Efficiency Ratio
     (path noise) does not measure.
   - **Multi-timeframe trend shape** — the regression R² (when the slope is up)
     on both a short and a long window, combined as their geometric mean, so a
     trend counts only if it holds across horizons and a recent spike (short up,
     long flat) is penalised.

The remaining components (momentum, regime, spread tightness) are the same soft
[0, 1] scores as ``open_ranking``.

On top of the geometric mean, a **pre-open bearish malus** guards against opening
into a market that is *already rolling over*: a least-squares fit of the bid over
the last few minutes right before opening (``recent_trend_period`` candles) yields
a multiplier in ``[recent_bearish_malus, 1]`` — ``1`` when flat/rising, dropping
toward the floor as the recent slide gets both steeper (relative decline vs.
``recent_drop_full_malus``) and cleaner (regression R²). It is applied
multiplicatively to the composite score, so a candidate whose earlier strength is
being undone by a fresh down-trend is dragged to the back of the ranking without
touching the other dimensions.

As in ``open_ranking``, :meth:`evaluate` returns ``None`` only on *structural*
grounds (too little data, non-positive bid, no measurable volatility), when fewer
than ``min_models_agree`` projection models point up, or when the composite falls
below the optional ``min_score`` floor.

Documented in ``docs/strategies/open_saferanking.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.core.indicators import (
    atr,
    efficiency_ratio,
    linear_regression,
    rate_of_change,
)
from src.core.projection import consensus
from src.entry.base import EntryIntent, EntryStrategy
from src.feed.price_buffer import EpicBuffer

logger = logging.getLogger(__name__)


def _clamp01(value: float) -> float:
    """Clamp ``value`` to the closed unit interval [0, 1]."""
    return max(0.0, min(1.0, value))


def _positive_slope_r2(values: list[float]) -> float:
    """Regression R² when the fit rises, else 0 — the "clean up-trend" score."""
    reg = linear_regression(values)
    return _clamp01(reg.r_squared) if reg.slope > 0 else 0.0


def _recent_bearish_factor(
    values: list[float], full_malus_drop: float, malus_floor: float
) -> float:
    """Score multiplier in ``[malus_floor, 1]`` penalising a recent down-trend.

    Looks at the *sliding bid over the last few minutes right before opening* and
    guards against crowning a market that is already rolling over. A least-squares
    fit of the recent bid gives both the direction (slope) and how *clean* the
    move is (R²):

    - a flat or rising fit (``slope >= 0``) returns ``1.0`` — no penalty;
    - a falling fit is penalised in proportion to the **relative decline** over
      the window (``-slope × span / last_bid``, capped at ``full_malus_drop``)
      **and** to the fit quality ``r_squared``, so only a *clear, steep* drop
      earns the full malus — a shallow or noisy wobble barely dents the score.

    The returned factor drops from ``1.0`` down to ``malus_floor`` as the
    down-trend gets both steeper and cleaner, and is applied multiplicatively to
    the composite ranking score.
    """
    if len(values) < 2 or full_malus_drop <= 0:
        return 1.0
    reg = linear_regression(values)
    if reg.slope >= 0:
        return 1.0  # flat or rising over the window: nothing to penalise
    last = values[-1]
    if last <= 0:
        return 1.0
    # Relative decline the fitted line describes over the whole window.
    decline = (-reg.slope) * (len(values) - 1) / last
    # Severity: how deep the drop is (vs. the full-malus threshold), gated by how
    # cleanly the points actually follow that downward line.
    severity = _clamp01(decline / full_malus_drop) * _clamp01(reg.r_squared)
    return 1.0 - severity * (1.0 - malus_floor)


def _pullback_safety(values: list[float]) -> float:
    """How monotone the rise is: ``1 - max_drawdown / range`` in [0, 1].

    ``max_drawdown`` is the deepest peak-to-trough drop along the curve (the worst
    adverse excursion a holder would have suffered); ``range`` is the full
    high-to-low span over the window. A straight climb has a near-zero drawdown
    and scores ~1; a rise interrupted by deep retracements scores low. Returns
    ``0.0`` for a flat/degenerate curve (no range to normalise against).
    """
    if len(values) < 2:
        return 0.0
    lo = min(values)
    hi = max(values)
    span = hi - lo
    if span <= 0:
        return 0.0

    peak = values[0]
    max_drawdown = 0.0
    for v in values:
        if v > peak:
            peak = v
        max_drawdown = max(max_drawdown, peak - v)

    return _clamp01(1.0 - max_drawdown / span)


@dataclass
class OpenSafeRanking(EntryStrategy):
    """Conjunctive cross-epic ranker — every dimension of a safe rise must hold."""

    name = "open_saferanking"
    cross_epic_selection = True

    # Rolling-selection constants (read by the scheduler). Plain class attributes
    # — not dataclass fields, not settings — so they stay constants of the
    # strategy. The strategy is selected at runtime from the dashboard.
    #
    # This ranker is *wallet-bounded*: instead of holding a single rolling
    # position, it keeps opening the best-ranked affordable epics until the
    # spendable balance (available funds minus ``wallet_reserve``) can no longer
    # cover another margin. ``concurrent_positions`` therefore only acts as a
    # conservative fallback cap for a pass when the account balance can't be read.
    wallet_bounded = True  # open epics as long as the wallet has funds
    concurrent_positions = 1  # fallback cap only, used when the balance is unknown
    open_after_minutes = 60  # ≈ one hour of livestream warm-up before first open
    wallet_reserve = 0.10  # keep 10% of available funds free
    min_participation_ratio = 0.5  # > half the warmed-up universe before crowning

    # Component windows.
    projection_horizon: int = 60  # candles ahead each projection model extends
    projection_degree: int = 2  # polynomial-model degree
    projection_ema_span: int = 10  # EMA-model span
    regression_period: int = 30  # candles for the short trend-shape R² fit
    regression_period_long: int = 60  # candles for the long trend-shape R² fit
    roc_period: int = 10  # momentum lookback (candles)
    roc_target: float = 0.5  # ROC% earning a full momentum score
    efficiency_period: int = 30  # ER regime window
    drawdown_period: int = 60  # window for the pull-back-safety drawdown scan
    atr_period: int = 14  # volatility check (needs a positive ATR to size a stop)
    max_spread_ratio: float = 0.0015  # spread/bid at which the spread score hits 0

    # Robustness knobs specific to this ranker.
    min_models_agree: int = 2  # structural gate: ≥ this many models must point up
    epsilon: float = 1e-3  # per-component floor keeping the geo-mean rankable
    min_score: float = 0.0  # composite floor; below it -> stay flat (0 = never)

    # Pre-open safety: penalise a market whose bid is *already sliding down* over
    # the last few minutes right before opening. Applied as a multiplicative malus
    # on the final composite score (not a geo-mean component), so a clear recent
    # down-trend can drive an otherwise-attractive candidate to the back of the
    # ranking without touching the other dimensions.
    recent_trend_period: int = 10  # candles (~10 min) of bid scanned before open
    recent_drop_full_malus: float = 0.003  # relative decline earning the full malus
    recent_bearish_malus: float = 0.05  # score multiplier floor at a clear steep drop

    # Per-projection-model weights (passed to the consensus). Setting all but one
    # to zero reduces the projection component to a single mathematical model.
    projection_weights: dict[str, float] = field(
        default_factory=lambda: {
            "linear": 0.40,
            "polynomial": 0.30,
            "ema": 0.30,
            "exp": 0.0,
        }
    )

    # Composite-score weights — the exponents of the weighted geometric mean.
    # They sum to 1.0 so the composite stays in [0, 1]. Projection stays the
    # dominant driver, but the two "safety" dimensions (multi-timeframe shape +
    # pull-back safety) together carry as much weight, so a fragile rise is
    # dragged down rather than rescued.
    weight_projection: float = 0.35
    weight_shape: float = 0.20
    weight_safety: float = 0.20
    weight_momentum: float = 0.10
    weight_regime: float = 0.10
    weight_spread: float = 0.05

    @property
    def warmup(self) -> int:
        return (
            max(
                self.projection_horizon,
                self.regression_period,
                self.regression_period_long,
                self.roc_period,
                self.efficiency_period,
                self.drawdown_period,
                self.atr_period,
                self.recent_trend_period,
            )
            + 1
        )

    @classmethod
    def from_settings(cls, settings) -> OpenSafeRanking:
        # All parameters are constants of this class (the dataclass field
        # defaults above), so the strategy builds from those and ignores
        # ``settings``. Tune the strategy by editing the constants here, and
        # select it at runtime from the dashboard.
        return cls()

    def _geometric_mean(self, components: list[tuple[float, float]]) -> float:
        """Weighted geometric mean of ``(value, weight)`` pairs, each floored.

        Every value is floored at ``epsilon`` before being raised to its weight,
        so the product stays strictly positive and monotone (rankable) while a
        near-zero component still collapses the composite toward the floor.
        """
        product = 1.0
        for value, weight in components:
            product *= max(self.epsilon, value) ** weight
        return product

    def evaluate(self, epic: str, buf: EpicBuffer) -> EntryIntent | None:
        candles = list(buf.candles)
        if len(candles) < self.warmup:
            return None  # not enough history to score the curve
        last = candles[-1]
        bid = last.bid_close
        spread = last.spread
        if bid <= 0:
            return None

        # A positive ATR is required structurally: without volatility the composed
        # close profile cannot size a protective stop at open.
        if atr(candles, self.atr_period) <= 0:
            return None

        bids = buf.bid_closes

        # 1. Projection, breadth-scaled — weighted multi-model agreement scaled by
        #    the *fraction* of models that actually point up. Rewards unanimity,
        #    discounts a lone confident model.
        projection = consensus(
            bids,
            direction="BUY",
            horizon=self.projection_horizon,
            weights=self.projection_weights,
            reference=bid,
            degree=self.projection_degree,
            ema_span=self.projection_ema_span,
        )
        # Structural gate: refuse a market almost no model projects upward.
        if projection.agree < self.min_models_agree:
            logger.debug(
                "SafeRanking %s rejected: only %d/%d models agree (< %d)",
                epic,
                projection.agree,
                projection.active,
                self.min_models_agree,
            )
            return None
        breadth = projection.agree / projection.active if projection.active else 0.0
        projection_score = projection.score * breadth

        # 2. Trend shape — R² of a positive-slope fit on BOTH a short and a long
        #    window, combined as their geometric mean so the trend must hold
        #    across horizons (a recent spike scores low).
        shape_short = _positive_slope_r2(bids[-self.regression_period :])
        shape_long = _positive_slope_r2(bids[-self.regression_period_long :])
        shape = (
            max(self.epsilon, shape_short) * max(self.epsilon, shape_long)
        ) ** 0.5

        # 3. Pull-back safety — 1 - max_drawdown/range over the window; the "safe"
        #    core rewarding a monotone climb and punishing deep retracements.
        safety = _pullback_safety(bids[-self.drawdown_period :])

        # 4. Momentum — positive ROC mapped onto [0, 1] against the target.
        roc = rate_of_change(bids, self.roc_period)
        momentum = _clamp01(roc / self.roc_target) if self.roc_target > 0 else 0.0

        # 5. Regime — Kaufman Efficiency Ratio (trend vs. chop), already [0, 1].
        regime = efficiency_ratio(buf.mid_closes, self.efficiency_period)

        # 6. Spread tightness — 1 at zero spread, 0 at/above the ceiling.
        spread_quality = (
            _clamp01(1.0 - (spread / bid) / self.max_spread_ratio)
            if self.max_spread_ratio > 0
            else 0.0
        )

        # Conjunctive combination: a weighted geometric mean, so a single weak
        # dimension drags the whole score down (no linear compensation).
        score = self._geometric_mean(
            [
                (projection_score, self.weight_projection),
                (shape, self.weight_shape),
                (safety, self.weight_safety),
                (momentum, self.weight_momentum),
                (regime, self.weight_regime),
                (spread_quality, self.weight_spread),
            ]
        )

        # Pre-open safety malus: if the bid has been sliding down over the last
        # few minutes, drag the score toward ``recent_bearish_malus`` so a market
        # already rolling over cannot win the ranking on its earlier strength.
        recent_factor = _recent_bearish_factor(
            bids[-self.recent_trend_period :],
            self.recent_drop_full_malus,
            self.recent_bearish_malus,
        )
        score *= recent_factor

        if score < self.min_score:
            return None

        logger.debug(
            "SafeRanking %s: score=%.3f (proj=%.2f×%.2f shape=%.2f safety=%.2f "
            "mom=%.2f regime=%.2f spread=%.2f recent×%.2f)",
            epic,
            score,
            projection.score,
            breadth,
            shape,
            safety,
            momentum,
            regime,
            spread_quality,
            recent_factor,
        )
        return EntryIntent(epic=epic, direction="BUY", score=score)
