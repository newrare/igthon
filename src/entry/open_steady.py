"""Cross-epic ranker — open the market whose last 10 minutes trace the
*cleanest* line, in either direction.

**Two-sided**: a clean rise is bought, a clean fall is sold. What is scored is
the **regularity** of the recent curve; the sign of its slope only picks the
side.

Like :class:`~src.entry.open_ranking.OpenRanking`,
:class:`~src.entry.open_linear.OpenLinear` and
:class:`~src.entry.open_slope.OpenSlope` this is a **ranker**, not a gate
(``cross_epic_selection = True``): the scheduler scores every tradable epic,
ranks them and opens the single best candidate. This module owns only the
*per-epic* half — "how regular and how visible is this curve over the last ten
minutes?" — and stays exit-agnostic (:meth:`evaluate` emits an
:class:`~src.entry.base.EntryIntent` carrying the direction and a comparable
score in [0, 1]; the stop/target/trailing belong to the composed
:class:`~src.exit.base.CloseProfile`).

The setup this ranker looks for
-------------------------------

The spec, translated: *score the last 10 minutes and keep the most regular and
visible curve. A clean, crisp trend is preferred over a very fast one that might
be a spike, and curves that go up and down all the time must be avoided.*

That is three distinct defects to reject, and — this is the reason the module is
not just :class:`~src.entry.open_slope.OpenSlope` with more weights — **no single
indicator rejects all three**:

======================  ==========  ==========  =============  ============
curve                   R²          Kaufman ER  largest step   net move
======================  ==========  ==========  =============  ============
regular line            **high**    **high**    **small**      visible
zig-zag (up-down-up)    low         **low**     small          small
one-candle spike        mediocre    **1.0**     **dominant**   visible
straight but flat       **high**    **high**    **small**      **~0**
======================  ==========  ==========  =============  ============

The spike row is the trap. The Efficiency Ratio is ``|net| / Σ|step|``, so a
single jump followed by silence has **ER = 1.0 — its maximum**: the choppiness
guard that catches the zig-zag *rewards* the spike. R² does not save it either (a
step function fits a line mediocrely, not terribly). Detecting a spike therefore
needs its own measure, and it is the ``smoothness`` component below.

Scoring — four direction-agnostic components
--------------------------------------------

All four are computed on the last ``window`` bid closes (~10 min on the
one-minute feed), so a long and a short candidate are ranked on the same scale.
The first three are *exactly* sign-free (they read magnitudes); ``visibility``
divides by the current bid, which a mirrored pair does not share, so it leaves a
sub-percent asymmetry between an up and a down candidate — real, but far below any
ranking decision:

1. **Linearity** — R² of the least-squares fit. "Régulière": the points sit on
   the line rather than merely starting and ending in the right place.
2. **Directness** — the Kaufman Efficiency Ratio over the same window. Penalises
   the up-and-down path directly, which R² partly tolerates.
3. **Smoothness (the anti-spike term)** — how evenly the travel is spread across
   the candles, from :func:`_step_concentration`: the largest single-candle move
   as a share of the window's total travel. A regular line of ``n`` points
   spreads its travel over ``n - 1`` equal steps, so the share sits at its
   structural minimum ``1 / (n - 1)``; a spike pushes it towards 1. The component
   maps ``1 / (n - 1)`` → 1.0 and ``max_step_share`` → 0.0, and a concentration
   *above* ``max_step_share`` is a hard veto: one candle carrying half the move is
   a pic, not a tendance.
4. **Visibility** — the fitted net move as a fraction of the current bid, softly
   saturating around ``move_target`` (:func:`_saturate`). The anti-flat qualifier:
   a curve can be perfectly straight, perfectly direct and perfectly smooth while
   going nowhere, which is not tradable in either direction. Expressed relative to
   the price so it is comparable across epics of any scale (an index moving in
   whole points and a forex pair in ten-thousandths saturate at the same relative
   travel). The saturation is *soft* rather than a clamp on purpose — a hard clamp
   ties every strong mover at exactly 1.0, and a ranker told to keep only the best
   would then choose arbitrarily among its top candidates.

A note on the division of labour, because it is counter-intuitive: ``smoothness``
measures how *evenly* the travel is spread, not how *good* the curve is, so a
zig-zag — whose steps are all the same size — scores a perfect 1.0 on it. That is
correct: the zig-zag is rejected by ``linearity`` (~0.06) and ``directness``
(~0.12), while the spike is rejected by ``smoothness`` alone. Each term covers a
defect the others miss, which is why all four are needed.

Composition — a weighted **geometric** mean, not a sum
------------------------------------------------------

The composite is ``Π componentᵢ ^ wᵢ`` with weights summing to 1, so it stays in
[0, 1] and reads as a percentage. The choice is deliberate and follows the lesson
already recorded in :class:`~src.entry.open_saferanking.OpenSafeRanking`: a
weighted **sum** is *compensatory*, so a large ``visibility`` could rescue a
zig-zag and crown exactly the curve the spec asks to avoid. A geometric mean is
bounded above by its smallest term, so **one weak dimension collapses the whole
score** — which is what "propre ET nette ET pas un pic ET pas en zigzag" means.
Each component is floored at ``epsilon`` so the composite stays strictly
monotone (still rankable among mediocre markets) instead of snapping ties to 0.

The three regularity terms carry ``0.85`` of the weight and ``visibility`` only
``0.15``, and ``visibility`` *saturates*: past ``move_target`` extra speed buys
no extra score. That is the spec's "*je préfère une tendance propre et nette
plutôt qu'une tendance très rapide*", made structural rather than advisory.

.. warning::

   **Unvalidated.** Unlike :class:`~src.entry.open_fade.OpenFade` (whose
   thresholds come from a replay over ~100 000 resolved outcomes), every constant
   below is a *reasoned starting point*, not a measured one — in particular
   ``min_score``, ``move_target`` and ``max_step_share``. Calibrate them on the
   simulator before drawing conclusions from live results.

Selection-layer behaviour
-------------------------

The spec's portfolio rules are class attributes read by the scheduler's rolling
selector:

- ``emits_shorts = True`` — genuinely two-sided, so the scheduler keeps SELL
  intents and lifts the long-only pre-open gate.
- ``min_period = 30`` (via :attr:`warmup`) — *au minimum 30 relevés consécutifs*
  before an epic is a ranking candidate. The candle *count* is the scheduler's
  warm-up test; contiguity is enforced here by :meth:`_is_contiguous`, since a
  stalled subscription leaves a gap the buffer length cannot reveal.
- ``min_participation_count = 21`` — *un classement n'est valide qu'avec plus de
  20 epics candidats*. An absolute floor, distinct from the ratio-based
  ``min_participation_ratio`` (disabled here), because the spec is a count.
- ``block_open_while_alive = True`` — *on n'ouvre rien de nouveau tant qu'un trade
  est vivant*: a position counts as **alive** once its software stop
  (``level_follower``) has ratcheted past ``level_margin`` while the close-out
  price is in profit, i.e. the gain is locked in. A position that is merely
  waiting — flat since open, drifting between break-even and its stop, or on its
  way to being stopped out — is **not** alive and deliberately does **not** block
  new opportunities.
- ``wallet_bounded = True`` + ``open_cooldown_minutes = 5`` — one open per pass,
  spaced by five minutes, while the spendable balance (available funds minus
  ``wallet_reserve``) covers another margin.

Documented in ``docs/strategies/open_steady.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from math import exp

from src.core.indicators import atr, efficiency_ratio, linear_regression
from src.entry.base import EntryIntent, EntryStrategy
from src.feed.price_buffer import EpicBuffer

logger = logging.getLogger(__name__)


def _clamp01(value: float) -> float:
    """Clamp ``value`` to the closed unit interval [0, 1]."""
    return max(0.0, min(1.0, value))


def _saturate(value: float, scale: float) -> float:
    """Map ``value >= 0`` into [0, 1), flattening past ``scale``.

    ``1 - exp(-value / scale)``: 0 at 0, ``0.63`` at ``scale``, ``0.86`` at twice
    it, ``0.95`` at three times. Used instead of a hard ``min(value / scale, 1)``
    clamp for the magnitude component, for two reasons:

    - it is **strictly increasing**, so two clean curves of different speed never
      tie. A hard clamp collapses every mover past ``scale`` to exactly 1.0, and a
      ranker that must "keep only the best" would then pick arbitrarily among its
      strongest candidates (whatever order the epics happen to arrive in);
    - the returns **diminish steeply**, which is the spec's stated preference: past
      the target, going four times faster is worth only a few score points, so a
      clean-but-moderate curve still outranks a violent one whose regularity terms
      are weaker.
    """
    if scale <= 0 or value <= 0:
        return 0.0
    return 1.0 - exp(-value / scale)


def _step_concentration(values: list[float]) -> float:
    """Share of the window's total travel carried by its single largest step.

    ``max|step| / Σ|step|``. A perfectly regular line of ``n`` values spreads its
    travel evenly over ``n - 1`` steps, so the share sits at its structural
    minimum ``1 / (n - 1)``; a one-candle spike carries most of the travel alone
    and pushes the share towards 1. This is the only one of the four components
    that separates a spike from a clean trend — the Efficiency Ratio scores a
    lone jump at its maximum 1.0.

    Returns 1.0 for a motionless window (no travel to spread), which reads as
    "not a trend" and is what the anti-flat ``visibility`` term also concludes.
    """
    if len(values) < 2:
        return 1.0
    steps = [abs(values[i] - values[i - 1]) for i in range(1, len(values))]
    travel = sum(steps)
    if travel <= 0:
        return 1.0
    return max(steps) / travel


@dataclass
class OpenSteady(EntryStrategy):
    """Rank markets by how regular and visible their last ~10 minutes are."""

    name = "open_steady"
    cross_epic_selection = True
    emits_shorts = True  # symmetric: buys a clean rise, sells a clean fall

    # Rolling-selection constants (read by the scheduler). Plain class attributes
    # — not dataclass fields, not settings — so they stay constants of the
    # strategy; it is selected at runtime via ``OPEN_STRATEGY``.
    wallet_bounded = True  # keep opening while the wallet covers another margin
    concurrent_positions = 1  # fallback cap only, used when the balance is unknown
    open_cooldown_minutes = 5  # one open per pass, ≥5 min apart
    wallet_reserve = 0.10  # keep 10% of available funds free
    # Ranking validity is an ABSOLUTE candidate count here ("plus de 20 epics
    # candidats"), not a fraction of the universe, so the ratio gate is disabled
    # and the count gate carries the rule. 21 = strictly more than 20.
    min_participation_ratio = 0.0
    min_participation_count = 21
    # Do not open anything new while an existing position already has its gain
    # locked in (software stop past the margin). A position still *waiting* for
    # its move does not block — see the module docstring.
    block_open_while_alive = True

    # Windows (candles ≈ minutes on the one-minute feed).
    window: int = 10  # the ~10 minutes the whole score is computed on
    atr_period: int = 14  # volatility window (gates stop sizing at open)
    # Minimum consecutive readings before an epic is a ranking candidate. Larger
    # than ``window`` on purpose: the spec wants a market that has been streaming
    # steadily for half an hour, not one that just came online with ten ticks.
    min_period: int = 30
    # Tolerance for "consécutifs": the largest gap allowed between two of the
    # last ``min_period`` candles. 90 s on a 60 s feed accepts normal jitter and
    # rejects a stalled subscription that silently left a hole in the curve.
    max_gap_seconds: float = 90.0

    # Anti-spike veto: the largest single-candle move may carry at most this share
    # of the window's total travel. 0.50 = one candle moving half the whole travel
    # is a spike, not a trend. Also the point where ``smoothness`` reaches 0.
    max_step_share: float = 0.50

    # Magnitude scale: the absolute fitted net move over the window as a fraction
    # of the current bid, around which ``visibility`` saturates. 0.0020 = a 0.20 %
    # move over ~10 minutes scores 0.63, twice that 0.86, three times 0.95 (see
    # ``_saturate``). Speed past the target therefore earns steeply diminishing
    # returns — the spec prefers clean over fast — while never tying two candidates.
    move_target: float = 0.0020

    # Composite weights — a weighted GEOMETRIC mean (see the module docstring),
    # summing to 1.0 so the score stays in [0, 1] and reads as a percentage. The
    # three regularity terms carry 0.85; visibility is the anti-flat qualifier.
    weight_linearity: float = 0.35  # R² of the window fit ("régulière")
    weight_directness: float = 0.25  # Kaufman ER (anti up-and-down)
    weight_smoothness: float = 0.25  # travel spread evenly (anti-spike)
    weight_visibility: float = 0.15  # relative net move (anti-flat)

    # Per-component floor keeping the geometric mean strictly positive and
    # rankable instead of collapsing mediocre markets to an unorderable 0.
    epsilon: float = 1e-3

    # Composite floor: below this the epic stays flat. A reasoned starting point,
    # NOT a measured one — a clean line scores ~0.90, a decent trend ~0.68 and a
    # zig-zag ~0.26 under the weights above, so 0.60 keeps genuine trends and
    # rejects noise. Calibrate on the simulator.
    min_score: float = 0.60

    @property
    def warmup(self) -> int:
        # The spec's 30 consecutive readings, and never fewer than the ATR window
        # needs (+1 for the true-range differencing) nor than the score window.
        return max(self.min_period, self.window, self.atr_period + 1)

    @classmethod
    def from_settings(cls, settings) -> OpenSteady:
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

    def _is_contiguous(self, candles: list) -> bool:
        """True when the last ``min_period`` candles carry no streaming gap.

        The buffer is a rolling deque appended to by the live feed, so its
        *length* says how many readings arrived, never whether they are
        consecutive: a stalled subscription leaves a hole that a regression reads
        as a genuine straight segment. This enforces the spec's "consécutifs" by
        checking the spacing of the readings the score is built on.
        """
        window = candles[-self.min_period :]
        for previous, current in zip(window, window[1:]):
            gap = (current.timestamp - previous.timestamp).total_seconds()
            if gap <= 0 or gap > self.max_gap_seconds:
                return False
        return True

    def evaluate(self, epic: str, buf: EpicBuffer) -> EntryIntent | None:
        candles = list(buf.candles)
        if len(candles) < self.warmup:
            return None  # fewer than ``min_period`` readings — not a candidate
        last = candles[-1]
        bid = last.bid_close
        if bid <= 0:
            return None

        # Gate — the readings must be consecutive, not merely numerous.
        if not self._is_contiguous(candles):
            logger.debug(
                "Steady %s rejected: gap in the last %d readings", epic, self.min_period
            )
            return None

        # A positive ATR is required structurally: without volatility the composed
        # close profile cannot size a protective stop at open.
        if atr(candles, self.atr_period) <= 0:
            return None

        window = buf.bid_closes[-self.window :]

        # The direction comes from the sign of the window's slope: a rising line is
        # bought, a falling one sold. Only an exactly flat fit gives no side to
        # take; a *nearly* flat one is held down by ``visibility`` below.
        reg = linear_regression(window)
        if reg.slope == 0:
            logger.debug("Steady %s rejected: flat window (zero slope)", epic)
            return None
        direction = "BUY" if reg.slope > 0 else "SELL"

        # Gate — anti-spike. One candle carrying more than ``max_step_share`` of
        # the travel is a pic, whatever the fit says about it.
        concentration = _step_concentration(window)
        if concentration > self.max_step_share:
            logger.debug(
                "Steady %s rejected: spike (largest step carries %.0f%% of the "
                "travel > %.0f%%)",
                epic,
                concentration * 100,
                self.max_step_share * 100,
            )
            return None

        # 1. Linearity — how well the window fits a straight line. Sign-free, so a
        #    fall is measured exactly as a climb.
        linearity = _clamp01(reg.r_squared)

        # 2. Directness — how directly the path travels its net distance (Kaufman
        #    ER, ``|net| / Σ|step|``), penalising the up-and-down curve the R² fit
        #    partly tolerates. ``len(window) - 1`` steps span exactly the window.
        directness = efficiency_ratio(window, len(window) - 1)

        # 3. Smoothness — the travel spread evenly across the candles. Maps the
        #    structural minimum ``1 / (n - 1)`` (a perfectly regular line) to 1.0
        #    and the veto threshold to 0.0. This is the only anti-spike measure:
        #    a lone jump scores ER = 1.0, so ``directness`` cannot catch it.
        ideal = 1.0 / (len(window) - 1)
        span = self.max_step_share - ideal
        smoothness = (
            _clamp01((self.max_step_share - concentration) / span) if span > 0 else 0.0
        )

        # 4. Visibility — the absolute fitted net move as a fraction of the bid, so
        #    a straight-but-flat line scores low whichever way it leans. Softly
        #    saturating rather than clamped (see :func:`_saturate`), so extra speed
        #    earns steeply diminishing returns without ever creating a tie at the
        #    top of the ranking.
        net_move = reg.slope * (len(window) - 1)
        visibility = _saturate(abs(net_move) / bid, self.move_target)

        score = self._geometric_mean(
            [
                (linearity, self.weight_linearity),
                (directness, self.weight_directness),
                (smoothness, self.weight_smoothness),
                (visibility, self.weight_visibility),
            ]
        )

        if score < self.min_score:
            logger.debug(
                "Steady %s below floor: %s score=%.3f < %.2f (linearity=%.2f "
                "directness=%.2f smoothness=%.2f visibility=%.2f)",
                epic,
                direction,
                score,
                self.min_score,
                linearity,
                directness,
                smoothness,
                visibility,
            )
            return None

        logger.debug(
            "Steady %s: %s score=%.3f (linearity=%.2f directness=%.2f "
            "smoothness=%.2f visibility=%.2f net_move=%.6g)",
            epic,
            direction,
            score,
            linearity,
            directness,
            smoothness,
            visibility,
            net_move,
        )
        return EntryIntent(epic=epic, direction=direction, score=score)
