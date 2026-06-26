"""Tests for the multi-model curve projection infrastructure
(src/core/projection.py).

These cover the pure maths only — fitting and extrapolating a curve, and the
weighted directional consensus — with no trading strategy involved.
"""

import math

from src.core.projection import (
    ConsensusResult,
    consensus,
    project_ema_slope,
    project_exponential,
    project_linear,
    project_polynomial,
)


def _line(n: int = 50, start: float = 100.0, step: float = 1.0) -> list[float]:
    return [start + i * step for i in range(n)]


class TestLinear:
    def test_projects_above_last_on_uptrend(self):
        values = _line(50, 100.0, 1.0)  # last = 149
        proj = project_linear(values, horizon=10)
        assert proj.model == "linear"
        # slope 1/candle, 10 ahead → ~159
        assert math.isclose(proj.projected, 159.0, abs_tol=1e-6)
        assert proj.confidence > 0.99  # a perfect line

    def test_downtrend_projects_below_last(self):
        proj = project_linear(_line(50, 100.0, -1.0), horizon=10)
        assert proj.projected < 100.0 - 49.0  # below the last point (51)

    def test_insufficient_data_is_zero_confidence(self):
        proj = project_linear([100.0], horizon=5)
        assert proj.confidence == 0.0


class TestPolynomial:
    def test_falls_back_to_linear_when_too_few_points(self):
        proj = project_polynomial([100.0, 101.0], horizon=5, degree=2)
        # degree+1 = 3 points needed → fall back to linear projector
        assert proj.model == "linear"

    def test_fits_a_parabola_better_than_a_line(self):
        # Accelerating curve: y = x^2.
        values = [float(i * i) for i in range(30)]
        poly = project_polynomial(values, horizon=5, degree=2)
        line = project_linear(values, horizon=5)
        assert poly.confidence >= line.confidence
        # An upward-accelerating curve projects above the last value.
        assert poly.projected > values[-1]

    def test_high_confidence_on_clean_quadratic(self):
        values = [2.0 * i * i - 3.0 * i + 5.0 for i in range(40)]
        proj = project_polynomial(values, horizon=3, degree=2)
        assert proj.confidence > 0.99


class TestEmaSlope:
    def test_uptrend_projects_up(self):
        proj = project_ema_slope(_line(60, 100.0, 0.5), horizon=10, span=10)
        assert proj.model == "ema"
        assert proj.projected > 100.0

    def test_high_confidence_on_straight_line(self):
        proj = project_ema_slope(_line(60), horizon=5, span=10)
        assert proj.confidence > 0.9


class TestExponential:
    def test_compounding_growth_projects_up(self):
        values = [100.0 * (1.01**i) for i in range(40)]
        proj = project_exponential(values, horizon=10)
        assert proj.model == "exp"
        assert proj.projected > values[-1]
        assert proj.confidence > 0.99

    def test_non_positive_values_fall_back_to_linear(self):
        proj = project_exponential([1.0, 0.0, -1.0, 2.0], horizon=5)
        assert proj.model == "linear"


class TestConsensus:
    def test_all_models_agree_on_clean_uptrend_scores_high(self):
        result = consensus(
            _line(60, 100.0, 1.0),
            direction="BUY",
            horizon=10,
            weights={"linear": 0.4, "polynomial": 0.3, "ema": 0.3, "exp": 0.0},
        )
        assert isinstance(result, ConsensusResult)
        assert result.active == 3  # exp disabled (weight 0)
        assert result.agree == 3
        assert result.score > 0.9

    def test_uptrend_with_sell_direction_scores_zero(self):
        result = consensus(
            _line(60, 100.0, 1.0),
            direction="SELL",
            horizon=10,
            weights={"linear": 0.5, "ema": 0.5},
        )
        assert result.agree == 0
        assert result.score == 0.0

    def test_zero_weight_models_are_skipped(self):
        result = consensus(
            _line(40),
            direction="BUY",
            horizon=5,
            weights={"linear": 1.0, "polynomial": 0.0, "ema": 0.0, "exp": 0.0},
        )
        assert result.active == 1
        assert [p.model for p in result.projections] == ["linear"]

    def test_score_is_weight_fraction_of_agreeing_models(self):
        # Linear (perfect line, weight 0.7) agrees with BUY; force the other
        # model to disagree by giving the EMA a downward reference well above it.
        values = _line(60, 100.0, 1.0)  # last = 159
        result = consensus(
            values,
            direction="BUY",
            horizon=10,
            weights={"linear": 0.7, "ema": 0.3},
            reference=10_000.0,  # nothing projects above this → all disagree
        )
        assert result.agree == 0
        assert result.score == 0.0

    def test_empty_curve_is_safe(self):
        result = consensus([], direction="BUY", horizon=5, weights={"linear": 1.0})
        assert result.score == 0.0
        assert result.active == 0
