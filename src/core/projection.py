"""Multi-model curve projection — shared, dependency-free math infrastructure.

This is pure plumbing (``core/``): it makes **no trading decision**. Given an
ordered price curve (typically the day's bid closes) it fits the curve with a
few independent mathematical models and extrapolates each one ``horizon``
candles into the future, returning a projected price and a [0, 1] confidence.

The models are deliberately diverse so that *agreement between them* carries
information a single fit cannot:

- :func:`project_linear` — least-squares straight line (slope × horizon). The
  baseline; confidence is the fit R².
- :func:`project_polynomial` — degree-``d`` least-squares fit (default 2),
  capturing curvature/acceleration the line misses.
- :func:`project_ema_slope` — extrapolates the local slope of an EMA, smoothing
  short-term noise; confidence is how straight the smoothed curve is (R²).
- :func:`project_exponential` — log-linear fit (compounding growth), projected
  back through ``exp``; confidence is the R² of the fit in log space.

:func:`consensus` combines a weighted set of these projections against a
candidate trade direction into a single opening score in [0, 1]: each model
contributes ``weight × confidence`` **only when its projected move agrees with
the direction**, so divergent models pull the score down rather than averaging
out. That score is the "theoretical projection verified across models" used to
gate an entry.

All maths is pure Python (no numpy) to match :mod:`src.core.indicators`.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from src.core.indicators import linear_regression

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Projection:
    """A single model's extrapolation of a curve ``horizon`` candles ahead.

    Attributes:
        model: Model key (``"linear"``, ``"polynomial"``, ``"ema"``, ``"exp"``).
        projected: Projected price at ``horizon`` candles past the last point.
        confidence: Quality of the underlying fit in [0, 1] (e.g. R²). ``0.0``
            when the model could not be fitted (insufficient/invalid data).
    """

    model: str
    projected: float
    confidence: float


@dataclass(slots=True)
class ConsensusResult:
    """Weighted multi-model agreement on a candidate direction.

    Attributes:
        score: Opening score in [0, 1] — ``sum(weight × confidence)`` over the
            models whose projected move agrees with the direction, divided by the
            total active weight. 0 when no model agrees, 1 when every active
            model agrees with full confidence.
        agree: Number of active models projecting in the candidate direction.
        active: Number of models with a positive weight that could be fitted.
        projections: Per-model projections (for logging/diagnostics).
    """

    score: float
    agree: int
    active: int
    projections: list[Projection]


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float] | None:
    """Solve a small linear system ``A x = b`` by Gaussian elimination.

    Args:
        matrix: Square coefficient matrix ``A`` (modified in place).
        rhs: Right-hand side vector ``b`` (modified in place).

    Returns:
        The solution vector ``x``, or ``None`` if the system is singular.
    """
    n = len(rhs)
    for col in range(n):
        # Partial pivot: pick the largest magnitude entry in this column.
        pivot = max(range(col, n), key=lambda r: abs(matrix[r][col]))
        if abs(matrix[pivot][col]) < 1e-12:
            return None
        matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
        rhs[col], rhs[pivot] = rhs[pivot], rhs[col]
        pivot_val = matrix[col][col]
        for r in range(n):
            if r == col:
                continue
            factor = matrix[r][col] / pivot_val
            for c in range(col, n):
                matrix[r][c] -= factor * matrix[col][c]
            rhs[r] -= factor * rhs[col]
    return [rhs[i] / matrix[i][i] for i in range(n)]


def _r_squared(values: list[float], predicted: list[float]) -> float:
    """Coefficient of determination of ``predicted`` against ``values``."""
    n = len(values)
    if n < 2:
        return 0.0
    mean_y = sum(values) / n
    ss_tot = sum((v - mean_y) ** 2 for v in values)
    if ss_tot <= 0:
        return 0.0
    ss_res = sum((v - p) ** 2 for v, p in zip(values, predicted))
    return max(0.0, 1.0 - ss_res / ss_tot)


def project_linear(values: list[float], horizon: int) -> Projection:
    """Project the curve with a least-squares straight line.

    Args:
        values: Ordered curve (oldest first); needs at least 2 points.
        horizon: Number of candles ahead of the last point to project.

    Returns:
        A :class:`Projection`; confidence is the regression R².
    """
    n = len(values)
    if n < 2 or horizon <= 0:
        return Projection("linear", values[-1] if values else 0.0, 0.0)
    reg = linear_regression(values)
    projected = reg.intercept + reg.slope * (n - 1 + horizon)
    return Projection("linear", projected, max(0.0, min(1.0, reg.r_squared)))


def project_polynomial(
    values: list[float], horizon: int, degree: int = 2
) -> Projection:
    """Project the curve with a degree-``degree`` least-squares polynomial.

    Captures curvature/acceleration the straight line misses. Falls back to the
    linear projection if the normal equations are singular.

    Args:
        values: Ordered curve (oldest first); needs at least ``degree + 1`` points.
        horizon: Number of candles ahead of the last point to project.
        degree: Polynomial degree (2 = parabola).

    Returns:
        A :class:`Projection`; confidence is the fit R².
    """
    n = len(values)
    if degree < 1 or n < degree + 1 or horizon <= 0:
        return project_linear(values, horizon)

    # Normalise x to [0, 1] over the sample so the powers stay well-conditioned;
    # the future point sits just past 1.0.
    xs = [i / (n - 1) for i in range(n)]
    x_future = (n - 1 + horizon) / (n - 1)

    # Normal equations A c = b for the least-squares polynomial coefficients.
    size = degree + 1
    powers = [[x**k for k in range(size)] for x in xs]
    matrix = [
        [sum(p[i] * p[j] for p in powers) for j in range(size)] for i in range(size)
    ]
    rhs = [sum(powers[r][i] * values[r] for r in range(n)) for i in range(size)]

    coeffs = _solve(matrix, rhs)
    if coeffs is None:
        return project_linear(values, horizon)

    def evaluate(x: float) -> float:
        return sum(c * x**k for k, c in enumerate(coeffs))

    fitted = [evaluate(x) for x in xs]
    projected = evaluate(x_future)
    return Projection("polynomial", projected, _r_squared(values, fitted))


def project_ema_slope(values: list[float], horizon: int, span: int = 10) -> Projection:
    """Project by extrapolating the local slope of an EMA of the curve.

    The EMA smooths short-term noise; its last-step slope is extended over the
    horizon. Confidence is how straight the smoothed curve is (R² of a line fit
    on the EMA), so a clean smooth trend scores high and a wobbly one low.

    Args:
        values: Ordered curve (oldest first); needs at least 2 points.
        horizon: Number of candles ahead of the last point to project.
        span: EMA span (larger = smoother, slower to react).

    Returns:
        A :class:`Projection`.
    """
    n = len(values)
    if n < 2 or horizon <= 0:
        return Projection("ema", values[-1] if values else 0.0, 0.0)

    alpha = 2.0 / (max(2, span) + 1)
    ema = values[0]
    series = [ema]
    for v in values[1:]:
        ema = alpha * v + (1 - alpha) * ema
        series.append(ema)

    slope = series[-1] - series[-2]
    projected = series[-1] + slope * horizon
    confidence = max(0.0, min(1.0, linear_regression(series).r_squared))
    return Projection("ema", projected, confidence)


def project_exponential(values: list[float], horizon: int) -> Projection:
    """Project with a log-linear (compounding-growth) model.

    Fits a straight line to ``log(values)`` and projects it back through
    ``exp``. Requires strictly positive values (prices); falls back to the
    linear projection otherwise. Confidence is the R² of the fit in log space.

    Args:
        values: Ordered curve (oldest first); needs at least 2 positive points.
        horizon: Number of candles ahead of the last point to project.

    Returns:
        A :class:`Projection`.
    """
    n = len(values)
    if n < 2 or horizon <= 0 or any(v <= 0 for v in values):
        return project_linear(values, horizon)
    logs = [math.log(v) for v in values]
    reg = linear_regression(logs)
    projected = math.exp(reg.intercept + reg.slope * (n - 1 + horizon))
    return Projection("exp", projected, max(0.0, min(1.0, reg.r_squared)))


#: Model key → projector callable. Extra knobs (degree, span) carry defaults.
PROJECTORS = {
    "linear": project_linear,
    "polynomial": project_polynomial,
    "ema": project_ema_slope,
    "exp": project_exponential,
}


def consensus(
    values: list[float],
    *,
    direction: str,
    horizon: int,
    weights: dict[str, float],
    reference: float | None = None,
    degree: int = 2,
    ema_span: int = 10,
) -> ConsensusResult:
    """Score how strongly the weighted models confirm a candidate direction.

    Each model with a positive weight is fitted and projected ``horizon`` candles
    ahead. A model **agrees** when its projected move (relative to ``reference``,
    the current price) points the same way as ``direction``. The score sums
    ``weight × confidence`` over agreeing models and divides by the total active
    weight, giving a [0, 1] figure: 0 = no model agrees, 1 = every active model
    agrees with a perfect fit. Disagreeing models contribute nothing, so genuine
    divergence (the "do not open" signal) pulls the score down.

    Args:
        values: Ordered curve (oldest first) — typically the day's bid closes.
        direction: Candidate trade direction, ``"BUY"`` or ``"SELL"``.
        horizon: Number of candles ahead to project.
        weights: Model key → weight. Models absent or with weight ≤ 0 are skipped.
        reference: Price the projection is compared against (default: last value).
        degree: Polynomial degree for the ``"polynomial"`` model.
        ema_span: EMA span for the ``"ema"`` model.

    Returns:
        A :class:`ConsensusResult`.
    """
    if not values:
        return ConsensusResult(0.0, 0, 0, [])
    ref = reference if reference is not None else values[-1]
    sign = 1.0 if direction == "BUY" else -1.0

    projections: list[Projection] = []
    total_weight = 0.0
    agree_weighted = 0.0
    agree = 0
    active = 0

    for model, weight in weights.items():
        if weight <= 0 or model not in PROJECTORS:
            continue
        if model == "polynomial":
            proj = project_polynomial(values, horizon, degree=degree)
        elif model == "ema":
            proj = project_ema_slope(values, horizon, span=ema_span)
        else:
            proj = PROJECTORS[model](values, horizon)
        projections.append(proj)
        active += 1
        total_weight += weight
        move = (proj.projected - ref) * sign
        if move > 0:
            agree += 1
            agree_weighted += weight * proj.confidence

    score = agree_weighted / total_weight if total_weight > 0 else 0.0
    return ConsensusResult(
        score=score, agree=agree, active=active, projections=projections
    )
