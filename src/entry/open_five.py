"""Cross-epic ranker — open a **series of five distinct shapes**, then wait.

A conjunctive, two-sided sibling of
:class:`~src.entry.open_saferanking.OpenSafeRanking` built around a different
*portfolio* model. Three things define it, and only the first is a per-epic
decision:

1. **Score every livestreamed epic and rank them** — a robust, direction-agnostic
   composite (weighted geometric mean, the ``open_saferanking`` lesson) that reads
   a clean rise as a BUY and a clean fall as a SELL, so the tournament ranks both
   sides on one comparable scale and simply keeps the best regardless of side.
2. **Open the top five at once** (:attr:`concurrent_positions` = 5,
   ``open_cooldown_minutes`` = 0), not one rolling position: the whole basket
   goes on in a single selection pass, at the same market moment.
3. **No new series until the book is empty** (``require_flat_book`` = True): while
   *any* of the five is still open the selector opens nothing, whatever its state.
   The next series waits for the last position of the previous one to close, so the
   strategy is judged on complete baskets rather than on a drip of overlapping
   trades whose results cannot be attributed to any one decision.

Why five ranked markets are not five bets
-----------------------------------------

Taking the top five of a tournament has a failure mode that a per-epic score
cannot see: **the best-scoring curves tend to be the same curve.** London cocoa
and New York cocoa quote the same commodity in two places; the CAC and the
EuroStoxx share most of their constituents; gold in dollars and gold in euros move
as one. When a market trends cleanly, so do its twins — they score alike and land
side by side at the top of the ranking. The resulting "diversified" basket of five
is then one position sized five times, and it does not merely concentrate the
upside: all five stops sit under the same move and fire on the same tick, so a
single adverse swing takes out the whole series at once.

Filtering on the epic string or the market description does not solve it. IG's
names are inconsistent between listings of one underlying, and unrelated markets
share words often enough to reject good candidates. So the duplicate test here is
purely mathematical, in :mod:`src.core.similarity`: each candidate curve is
reduced to a scale-free **signature** (the recent relative-return path, plus a
short ``fingerprint`` id for the logs), and two candidates are duplicates when
their **signed redundancy** — ``dir_a · dir_b · corr(returns)`` — exceeds
:attr:`max_shape_redundancy`. Signing by the two directions is what makes one
threshold cover both traps: two listings of one commodity bought together
(correlated curves, same side) *and* a mirrored pair like a long EUR/USD beside a
short USD/CHF (anti-correlated curves, opposite sides) both come out near ``+1``.
The filter runs in :meth:`filter_ranked` over the *whole* ranking, keeping the
better-ranked twin, so a duplicate at rank 3 promotes rank 6 into the basket
instead of shrinking it.

Scoring — the same conjunctive composite, made symmetric
-------------------------------------------------------

``OpenSafeRanking`` is long-only: its components read "is this rising?". Here each
one is mirrored around the candidate direction (``sign`` = +1 for a BUY, −1 for a
SELL), so a clean fall scores exactly as a clean rise of the same quality:

===============  ==========================================================
component        measures (in the trade's own direction)
===============  ==========================================================
``projection``   multi-model consensus for that side, scaled by the
                 *fraction* of models that agree (breadth beats one
                 over-confident model); a hard gate refuses a market fewer
                 than ``min_models_agree`` models support
``shape``        regression R² on a short **and** a long window, counted
                 only where the slope points the trade's way, combined as
                 their geometric mean so a lone recent spike is penalised
``safety``       ``1 − worst adverse excursion / range``: the deepest move
                 *against* the trade along the window (drawdown for a long,
                 run-up for a short) — the real risk a holder faced, which
                 the Efficiency Ratio does not measure
``momentum``     signed ROC mapped onto [0, 1] against ``roc_target``
``regime``       Kaufman Efficiency Ratio — trend versus chop, sign-free
``spread``       spread tightness, 1 at zero spread and 0 at the ceiling
===============  ==========================================================

They are combined as a **weighted geometric mean** (exponents summing to 1, so the
score stays in [0, 1]), not a weighted sum. A sum is compensatory — one strong
component rescues a weak market — while a geometric mean is bounded above by its
smallest term, so a single weak dimension collapses the score. Every component is
floored at ``epsilon`` to keep the composite strictly monotone, hence rankable
among mediocre markets rather than snapping ties to zero.

Two guards sit outside the composite:

- a **hard direction gate** — the least-squares slope over the whole buffered
  session *and* over the last :attr:`trend_gate_period` candles must share one
  strict sign, which both vetoes a market with no agreed direction and *chooses*
  the side. A soft penalty would not do: a ranker must open the best of its pool,
  so the least-bad directionless market would still be opened;
- a **counter-trend malus** — a least-squares fit of the last
  :attr:`recent_trend_period` candles yields a multiplier in
  ``[recent_counter_malus, 1]``, dropping toward the floor as the move against the
  intended side gets both steeper and cleaner (R²-gated). Applied
  multiplicatively, it drags a candidate whose earlier strength is already being
  undone to the back of the ranking without touching the other dimensions.

.. warning::

   **Unvalidated.** Every constant below is a *reasoned starting point*, not a
   measured one — in particular ``min_score``, ``max_shape_redundancy`` and
   ``signature_window``. Calibrate them on the simulator before drawing
   conclusions from live results.

Documented in ``docs/strategies/open_five.md``.
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
from src.core.similarity import ShapeSignature, deduplicate, shape_signature
from src.entry.base import EntryIntent, EntryStrategy
from src.feed.price_buffer import EpicBuffer

logger = logging.getLogger(__name__)


def _clamp01(value: float) -> float:
    """Clamp ``value`` to the closed unit interval [0, 1]."""
    return max(0.0, min(1.0, value))


def _directional_r2(values: list[float], sign: float) -> float:
    """Regression R² when the fit runs the trade's way, else 0.

    The "clean trend in my direction" score, mirrored: for a long (``sign`` = +1)
    only a rising fit counts, for a short only a falling one. A fit pointing the
    other way scores 0 whatever its quality — a tidy line against the trade is
    not a reason to take it.
    """
    reg = linear_regression(values)
    return _clamp01(reg.r_squared) if reg.slope * sign > 0 else 0.0


def _adverse_excursion_safety(values: list[float], sign: float) -> float:
    """How monotone the move is *for this side*: ``1 − adverse / range`` in [0, 1].

    The adverse excursion is the deepest move against the trade along the window
    — the maximum drawdown from a running peak for a long, the maximum run-up from
    a running trough for a short. Both are the same computation on the
    direction-signed curve (``sign × price``), which is why one function covers
    the two sides. ``range`` is the window's full span, so the result reads as
    "what fraction of the travel was given back at worst": a straight move scores
    ~1, one interrupted by violent retracements scores low.

    Returns ``0.0`` for a flat or degenerate curve (no range to normalise
    against), which reads as "not a trend" — the same conclusion the momentum and
    shape components reach.
    """
    if len(values) < 2:
        return 0.0
    span = max(values) - min(values)
    if span <= 0:
        return 0.0

    signed = [sign * v for v in values]
    extreme = signed[0]
    adverse = 0.0
    for value in signed:
        if value > extreme:
            extreme = value
        adverse = max(adverse, extreme - value)

    return _clamp01(1.0 - adverse / span)


def _counter_trend_factor(
    values: list[float], sign: float, full_malus_drop: float, malus_floor: float
) -> float:
    """Score multiplier in ``[malus_floor, 1]`` penalising a recent reversal.

    Reads the last few minutes right before opening and guards against joining a
    market that is *already turning against the intended side*. A least-squares
    fit gives both the direction of the recent move and how *clean* it is (R²):

    - a fit running the trade's way (or flat) returns ``1.0`` — nothing to
      penalise;
    - a fit running against it is penalised in proportion to the **relative
      move** over the window (capped at ``full_malus_drop``) **and** to
      ``r_squared``, so only a *clear, steep* reversal earns the full malus while
      a shallow or noisy wobble barely dents the score.

    Mirrored through ``sign``, so a short is judged by the same rule against a
    fresh rise as a long is against a fresh slide.
    """
    if len(values) < 2 or full_malus_drop <= 0:
        return 1.0
    reg = linear_regression(values)
    adverse_slope = -sign * reg.slope
    if adverse_slope <= 0:
        return 1.0  # the recent move runs the trade's way (or is flat)
    last = values[-1]
    if last <= 0:
        return 1.0
    # Relative move against the trade that the fitted line describes.
    move = adverse_slope * (len(values) - 1) / last
    severity = _clamp01(move / full_malus_drop) * _clamp01(reg.r_squared)
    return 1.0 - severity * (1.0 - malus_floor)


@dataclass
class OpenFive(EntryStrategy):
    """Rank every epic, open the best five *distinct* shapes, wait for a flat book."""

    name = "open_five"
    cross_epic_selection = True
    emits_shorts = True  # two-sided: a clean rise is bought, a clean fall is sold

    # Rolling-selection constants (read by the scheduler). Plain class attributes
    # — not dataclass fields, not settings — so they stay constants of the
    # strategy; it is selected at runtime via ``OPEN_STRATEGY``.
    #
    # The portfolio model is "series of five": count-bounded at five (NOT
    # wallet-bounded, so the basket size is the decision and the wallet only ever
    # trims it), the whole basket opened in one pass (no cooldown), and no new
    # series until every position of the previous one has closed.
    concurrent_positions = 5  # the basket size — the top five of the ranking
    wallet_bounded = False  # five is the target, not "as many as the wallet allows"
    open_cooldown_minutes = 0  # the five go on together, in the same pass
    require_flat_book = True  # a new series only from a completely empty book
    # Redundant beside ``require_flat_book`` (which blocks on *any* open position,
    # secured or not) — left off so the two rules are not confused for each other.
    block_open_while_alive = False
    wallet_reserve = 0.10  # keep 10% of available funds free
    # A basket of five drawn from a shallow pool is not a selection. Both
    # participation gates apply: more than half the livestreamed universe must be
    # warmed up, and at least 20 epics in absolute terms — four times the basket
    # size, so the ranking has something to choose from and the duplicate filter
    # has spares to promote.
    min_participation_ratio = 0.5
    min_participation_count = 20

    # --- Duplicate-shape filter (see :meth:`filter_ranked`) ------------------
    # Candles the signature is built on (~1 h on the one-minute feed). Long enough
    # for a correlation to mean something, short enough to describe *today's*
    # relationship rather than a historical average.
    signature_window: int = 60
    # Minimum timestamps two signatures must share for their correlation to be
    # trusted. Below this the pair is left alone (never dropped) — see the
    # abstention rule in :mod:`src.core.similarity`.
    signature_min_overlap: int = 20
    # Signed-redundancy veto: above this, a candidate is the same bet as one
    # already kept and is dropped from the basket. 0.80 is deliberately below the
    # ~0.95 of two listings of one commodity, so near-twins (two indices of the
    # same region, two crosses sharing a currency) are caught too.
    max_shape_redundancy: float = 0.80

    # --- Component windows ---------------------------------------------------
    projection_horizon: int = 60  # candles ahead each projection model extends
    projection_degree: int = 2  # polynomial-model degree
    projection_ema_span: int = 10  # EMA-model span
    regression_period: int = 30  # candles for the short trend-shape R² fit
    regression_period_long: int = 60  # candles for the long trend-shape R² fit
    roc_period: int = 10  # momentum lookback (candles)
    roc_target: float = 0.5  # absolute ROC% earning a full momentum score
    efficiency_period: int = 30  # ER regime window
    excursion_period: int = 60  # window for the adverse-excursion safety scan
    atr_period: int = 14  # volatility check (needs a positive ATR to size a stop)
    max_spread_ratio: float = 0.0015  # spread/bid at which the spread score hits 0

    # --- Gates and robustness knobs -----------------------------------------
    min_models_agree: int = 2  # structural gate: ≥ this many models must agree
    epsilon: float = 1e-3  # per-component floor keeping the geo-mean rankable
    min_score: float = 0.0  # composite floor; below it -> stay flat (0 = never)

    # Hard direction gate: the whole-session slope and the recent slope must share
    # one strict sign. That both vetoes a market with no agreed direction and
    # picks the side. Set False to rank on the composite alone (BUY/SELL then
    # follows the session slope only).
    require_agreed_trend: bool = True
    trend_gate_period: int = 20  # candles (~20 min) for the recent half of the gate

    # Counter-trend malus (multiplicative, outside the geometric mean).
    recent_trend_period: int = 10  # candles (~10 min) of bid scanned before open
    recent_move_full_malus: float = 0.003  # relative adverse move for the full malus
    recent_counter_malus: float = 0.05  # score multiplier floor at a clear reversal

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
    # adverse excursion) together carry as much weight, so a fragile move is
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
                self.excursion_period,
                self.atr_period,
                self.recent_trend_period,
                self.trend_gate_period,
                self.signature_window,
            )
            + 1
        )

    @classmethod
    def from_settings(cls, settings) -> OpenFive:
        # All parameters are constants of this class (the dataclass field defaults
        # above), so the strategy builds from those and ignores ``settings``. Tune
        # by editing the constants here; select it at runtime via ``OPEN_STRATEGY``.
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

    def _direction(self, epic: str, bids: list[float]) -> str | None:
        """Side to trade from the two trend horizons, or ``None`` to skip the epic.

        The whole buffered session and the last :attr:`trend_gate_period` candles
        must agree on a strict sign: up on both is a BUY, down on both a SELL, and
        anything else (either horizon flat, or the two disagreeing) means the
        market has no direction to join. Disagreement is the falling-knife case in
        both mirrors — a curve that climbed all morning but has been sliding into
        the open, or the reverse — which is why it is a veto and not a penalty.
        """
        day_slope = linear_regression(bids).slope
        recent_slope = linear_regression(bids[-self.trend_gate_period :]).slope
        if not self.require_agreed_trend:
            return "BUY" if day_slope > 0 else "SELL" if day_slope < 0 else None
        if day_slope > 0 and recent_slope > 0:
            return "BUY"
        if day_slope < 0 and recent_slope < 0:
            return "SELL"
        logger.debug(
            "Five %s rejected: no agreed direction (session slope %.5g, "
            "recent slope %.5g)",
            epic,
            day_slope,
            recent_slope,
        )
        return None

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

        direction = self._direction(epic, bids)
        if direction is None:
            return None
        sign = 1.0 if direction == "BUY" else -1.0

        # 1. Projection, breadth-scaled — weighted multi-model agreement for this
        #    side, scaled by the *fraction* of models that actually point that way.
        projection = consensus(
            bids,
            direction=direction,
            horizon=self.projection_horizon,
            weights=self.projection_weights,
            reference=bid,
            degree=self.projection_degree,
            ema_span=self.projection_ema_span,
        )
        # Structural gate: refuse a market almost no model supports.
        if projection.agree < self.min_models_agree:
            logger.debug(
                "Five %s rejected: only %d/%d models agree on %s (< %d)",
                epic,
                projection.agree,
                projection.active,
                direction,
                self.min_models_agree,
            )
            return None
        breadth = projection.agree / projection.active if projection.active else 0.0
        projection_score = projection.score * breadth

        # 2. Trend shape — R² of a fit running the trade's way on BOTH a short and
        #    a long window, as their geometric mean so the trend must hold across
        #    horizons (a recent spike scores low).
        shape_short = _directional_r2(bids[-self.regression_period :], sign)
        shape_long = _directional_r2(bids[-self.regression_period_long :], sign)
        shape = (max(self.epsilon, shape_short) * max(self.epsilon, shape_long)) ** 0.5

        # 3. Adverse-excursion safety — how much of the travel was given back
        #    against the trade, mirrored for a short.
        safety = _adverse_excursion_safety(bids[-self.excursion_period :], sign)

        # 4. Momentum — ROC in the trade's direction, mapped onto [0, 1].
        roc = rate_of_change(bids, self.roc_period) * sign
        momentum = _clamp01(roc / self.roc_target) if self.roc_target > 0 else 0.0

        # 5. Regime — Kaufman Efficiency Ratio (trend vs. chop), sign-free, [0, 1].
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

        # Counter-trend malus: if the last few minutes already run against the side
        # being taken, drag the score toward ``recent_counter_malus`` so a market
        # that is turning cannot win the ranking on its earlier strength.
        counter_factor = _counter_trend_factor(
            bids[-self.recent_trend_period :],
            sign,
            self.recent_move_full_malus,
            self.recent_counter_malus,
        )
        score *= counter_factor

        if score < self.min_score:
            return None

        logger.debug(
            "Five %s: %s score=%.3f (proj=%.2f×%.2f shape=%.2f safety=%.2f "
            "mom=%.2f regime=%.2f spread=%.2f counter×%.2f)",
            epic,
            direction,
            score,
            projection.score,
            breadth,
            shape,
            safety,
            momentum,
            regime,
            spread_quality,
            counter_factor,
        )
        return EntryIntent(epic=epic, direction=direction, score=score)

    def filter_ranked(
        self, ranked: list[tuple[EntryIntent, EpicBuffer]]
    ) -> list[tuple[EntryIntent, EpicBuffer]]:
        """Drop the shape duplicates from the ranking, keeping the better-ranked twin.

        The basket is only diversified if its five members are five *different*
        bets, and ranking alone does not ensure that: when a commodity trends, its
        other listing trends identically, so twins score alike and cluster at the
        top (see the module docstring). Each candidate curve is reduced to a
        scale-free signature and compared with every candidate already kept; one
        whose **signed redundancy** exceeds :attr:`max_shape_redundancy` with any
        of them is refused.

        Runs over the *whole* ranking rather than the top five, so a duplicate at
        rank 3 promotes rank 6 into the basket instead of leaving a hole — the
        scheduler then opens the first five survivors that clear its own gates.
        Order is preserved, so the surviving twin is always the better-ranked one.

        A candidate whose signature cannot be built (too short a curve, a
        non-positive price) is kept **unfiltered** rather than dropped: a data
        problem must not silently shrink the basket. Pairs whose curves cannot be
        compared are likewise left alone (see :mod:`src.core.similarity`).
        """
        positions: list[int] = []
        items: list[tuple[ShapeSignature, str]] = []
        for index, (intent, buf) in enumerate(ranked):
            signature = shape_signature(
                intent.epic, list(buf.candles), self.signature_window
            )
            if signature is None:
                logger.warning(
                    "Five %s: no shape signature — kept without a duplicate check",
                    intent.epic,
                )
                continue
            positions.append(index)
            items.append((signature, intent.direction))

        kept, dropped = deduplicate(
            items,
            max_redundancy=self.max_shape_redundancy,
            min_overlap=self.signature_min_overlap,
        )
        if not dropped:
            logger.debug(
                "Five: %d candidate(s), no duplicate shape (fingerprints %s)",
                len(items),
                ", ".join(
                    f"{items[i][0].epic}:{items[i][0].fingerprint}" for i in kept
                ),
            )
            return ranked

        refused = set()
        for drop in dropped:
            duplicate = items[drop.index][0]
            twin = items[drop.against][0]
            refused.add(positions[drop.index])
            logger.info(
                "Five: dropping %s (%s) — same shape as %s (%s), redundancy %.2f "
                "> %.2f",
                duplicate.epic,
                duplicate.fingerprint,
                twin.epic,
                twin.fingerprint,
                drop.redundancy,
                self.max_shape_redundancy,
            )
        return [item for index, item in enumerate(ranked) if index not in refused]
