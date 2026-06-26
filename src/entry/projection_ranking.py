"""Cross-epic ranker — keep one rolling position on the most *promising* market.

Unlike the per-epic breakout entries (``donchian_er`` / ``donchian_projection``),
this strategy is a **ranker**, not a gate (``cross_epic_selection = True``). The
scheduler maintains a target number of open positions (default 1, a single
rolling position): an hour into the session it scores every tradable epic, ranks
them by the score this strategy returns and opens the best affordable one; the
moment that position closes — win or loss — it re-ranks and re-opens, so the
account is in the market all day. The cross-epic comparison and the
replace-on-close cadence live in the orchestration layer — this module owns only
the *per-epic* half: "how promising/rising is this curve right now?".

It stays true to the open/close decoupling: :meth:`evaluate` emits an
:class:`~src.entry.base.EntryIntent` carrying the direction (BUY only — the live
pipeline is long-only) and the composite **opening score**; it says nothing about
the stop/target/trailing, which belong to the composed
:class:`~src.exit.base.CloseProfile`.

Scoring — several independent mathematical tools, each normalised to [0, 1]
(higher = more promising) and combined with weights that sum to 1, so the
resulting score is itself in [0, 1] and directly comparable across epics:

1. **Projection (dominant).** The multi-model consensus of
   :mod:`src.core.projection` — the day's bid curve is fitted by several
   independent models (linear, polynomial, EMA-slope, log-linear), each
   extrapolated ``projection_horizon`` candles ahead, and the weighted,
   confidence-scaled fraction projecting **up** is the score. This is the
   "trade montant / prometteur" core: a market several diverse models agree is
   rising ranks highest.
2. **Trend shape.** The R² of a linear regression over ``regression_period`` bid
   closes *when the slope is positive* (else 0) — a clean, straight up-trend.
3. **Momentum.** Rate of change over ``roc_period`` candles, mapped onto [0, 1]
   against ``roc_target`` (the ROC%, in percent, that earns a full momentum
   score); only positive momentum counts.
4. **Regime.** The Kaufman Efficiency Ratio over ``efficiency_period`` candles
   (already in [0, 1]): trend vs. chop.
5. **Spread tightness.** ``1 - (spread / bid) / max_spread_ratio`` clamped to
   [0, 1]: a tie-breaker favouring cheaper-to-trade markets.

These are **soft components, not pass/fail gates** — the goal is to open one
epic every hour, so a weak criterion lowers the rank rather than rejecting the
market. :meth:`evaluate` returns ``None`` only on *structural* grounds (too
little data, non-positive bid, no measurable volatility) or when the composite
falls below the optional ``min_score`` floor (default 0 = never floor).

Documented in ``docs/strategies/projection-ranking.md``.
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


@dataclass
class ProjectionRankingEntry(EntryStrategy):
    """Score each epic's promise; the scheduler opens the best one hourly."""

    name = "projection_ranking"
    cross_epic_selection = True

    # Rolling-selection constants (read by the scheduler). Plain class attributes
    # — not dataclass fields, not settings — so they stay constants of the
    # strategy. The strategy is selected at runtime from the dashboard.
    concurrent_positions = 1  # keep a single rolling position open all day
    open_after_minutes = 60  # ≈ one hour of livestream warm-up before first open
    wallet_reserve = 0.10  # keep 10% of available funds free

    # Component windows.
    projection_horizon: int = 60  # candles ahead each projection model extends
    projection_degree: int = 2  # polynomial-model degree
    projection_ema_span: int = 10  # EMA-model span
    regression_period: int = 30  # candles for the trend-shape R² fit
    roc_period: int = 10  # momentum lookback (candles)
    roc_target: float = 0.5  # ROC% earning a full momentum score
    efficiency_period: int = 30  # ER regime window
    atr_period: int = 14  # volatility check (needs a positive ATR to size a stop)
    max_spread_ratio: float = 0.0015  # spread/bid at which the spread score hits 0
    min_score: float = 0.0  # composite floor; below it -> stay flat (0 = never)

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

    # Composite-score weights — projection-dominant, the rest as tie-breakers.
    # They sum to 1.0 so the composite stays in [0, 1].
    weight_projection: float = 0.40
    weight_shape: float = 0.25
    weight_momentum: float = 0.15
    weight_regime: float = 0.10
    weight_spread: float = 0.10

    @property
    def warmup(self) -> int:
        return (
            max(
                self.projection_horizon,
                self.regression_period,
                self.roc_period,
                self.efficiency_period,
                self.atr_period,
            )
            + 1
        )

    @classmethod
    def from_settings(cls, settings) -> ProjectionRankingEntry:
        # All parameters are constants of this class (the dataclass field
        # defaults above), so the strategy builds from those and ignores
        # ``settings``. Tune the strategy by editing the constants here, and
        # select it at runtime from the dashboard.
        return cls()

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

        # 1. Projection (dominant) — weighted multi-model agreement that the bid
        #    curve is rising. Already in [0, 1], confidence-scaled.
        projection = consensus(
            bids,
            direction="BUY",
            horizon=self.projection_horizon,
            weights=self.projection_weights,
            reference=bid,
            degree=self.projection_degree,
            ema_span=self.projection_ema_span,
        )
        projection_score = projection.score

        # 2. Trend shape — R² of the regression when it genuinely rises.
        reg = linear_regression(bids[-self.regression_period :])
        shape = reg.r_squared if reg.slope > 0 else 0.0

        # 3. Momentum — positive ROC mapped onto [0, 1] against the target.
        roc = rate_of_change(bids, self.roc_period)
        momentum = _clamp01(roc / self.roc_target) if self.roc_target > 0 else 0.0

        # 4. Regime — Kaufman Efficiency Ratio (trend vs. chop), already [0, 1].
        regime = efficiency_ratio(buf.mid_closes, self.efficiency_period)

        # 5. Spread tightness — 1 at zero spread, 0 at/above the ceiling.
        spread_quality = (
            _clamp01(1.0 - (spread / bid) / self.max_spread_ratio)
            if self.max_spread_ratio > 0
            else 0.0
        )

        score = (
            self.weight_projection * projection_score
            + self.weight_shape * shape
            + self.weight_momentum * momentum
            + self.weight_regime * regime
            + self.weight_spread * spread_quality
        )

        if score < self.min_score:
            return None

        logger.debug(
            "ProjectionRanking %s: score=%.3f (proj=%.2f shape=%.2f mom=%.2f "
            "regime=%.2f spread=%.2f)",
            epic,
            score,
            projection_score,
            shape,
            momentum,
            regime,
            spread_quality,
        )
        return EntryIntent(epic=epic, direction="BUY", score=score)
