"""Donchian breakout, gated by the Efficiency Ratio *and* a multi-model
projection consensus.

This is :class:`~src.entry.open_donchian.OpenDonchian` with one extra, harder
gate bolted on after the regime check. It is the *open* side only: it decides
**whether and which way to enter** and emits an
:class:`~src.entry.base.EntryIntent` carrying the direction; it says nothing
about the stop/target/trailing — those belong to the
:class:`~src.exit.base.CloseProfile` composed with it at runtime.

Pipeline (each step must pass, in order):

1. **Spread gate** — ``spread / bid`` under ``max_spread_ratio``.
2. **Regime gate** — Kaufman Efficiency Ratio over ``efficiency_period`` ≥
   ``min_efficiency`` (skip chop, keep clean trends).
3. **Volatility check** — ATR over ``atr_period`` must be positive.
4. **Donchian breakout** — the bid closes outside the prior ``channel``-period
   high/low band → candidate direction.
5. **Projection consensus (the addition)** — the day's bid curve is fitted by
   several independent mathematical models (linear, polynomial, EMA-slope,
   log-linear) and each is extrapolated ``projection_horizon`` candles ahead.
   The weighted fraction of models whose projected move agrees with the breakout
   direction (scaled by each fit's confidence) is the **opening score**. The
   entry is taken only when that score reaches ``min_projection_score`` — i.e.
   the theoretical projection has to *confirm*, across models, the direction the
   breakout suggests. The score is surfaced on the intent.

Model weights and the horizon/threshold are configured in
:class:`~src.core.config.Settings`; setting all weights but one to zero reduces
the gate to a single mathematical model.

Documented in ``docs/strategies/open_projection.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.core.indicators import atr, efficiency_ratio
from src.core.projection import consensus
from src.entry.base import EntryIntent, EntryStrategy
from src.feed.price_buffer import EpicBuffer

logger = logging.getLogger(__name__)


@dataclass
class OpenProjection(EntryStrategy):
    """Donchian breakout confirmed by a weighted multi-model curve projection."""

    name = "open_projection"

    channel: int = 20  # Donchian lookback (prior candles forming the band)
    atr_period: int = 14  # confirms there is measurable volatility
    efficiency_period: int = 30  # ER lookback window
    min_efficiency: float = 0.60  # regime gate threshold (0 disables)
    max_spread_ratio: float = 0.0010  # tightened: a breakout edge dies on spread

    # Projection consensus gate.
    projection_horizon: int = 30  # candles ahead each model extrapolates
    projection_degree: int = 2  # polynomial-model degree
    projection_ema_span: int = 10  # EMA-model span
    min_projection_score: float = 0.50  # min weighted consensus to open
    # Model weights — set one to the only non-zero value to use a single model.
    projection_weights: dict[str, float] = field(
        default_factory=lambda: {
            "linear": 0.40,
            "polynomial": 0.30,
            "ema": 0.30,
            "exp": 0.0,
        }
    )

    @property
    def warmup(self) -> int:
        return (
            max(
                self.channel,
                self.efficiency_period,
                self.atr_period,
                self.projection_horizon,
            )
            + 1
        )

    @classmethod
    def from_settings(cls, settings) -> OpenProjection:
        # Parameters are constants of this class (the field defaults above), so
        # the strategy builds from those and ignores ``settings``. Tune by
        # editing the constants; select the strategy at runtime from the dashboard.
        return cls()

    def evaluate(self, epic: str, buf: EpicBuffer) -> EntryIntent | None:
        candles = list(buf.candles)
        if len(candles) < self.warmup:
            return None
        last = candles[-1]
        bid = last.bid_close
        spread = last.spread
        if bid <= 0:
            return None

        # Gate 1 — spread: a breakout edge dies under a wide spread.
        if spread / bid > self.max_spread_ratio:
            return None

        # Gate 2 — regime: only arm the breakout on efficiently trending paths.
        er = efficiency_ratio(buf.mid_closes, self.efficiency_period)
        if er < self.min_efficiency:
            return None

        # Confirm there is measurable volatility before trading the band.
        if atr(candles, self.atr_period) <= 0:
            return None

        # Donchian band from the candles *before* the current one.
        prior = candles[-self.channel - 1 : -1]
        band_high = max(c.bid_high for c in prior)
        band_low = min(c.bid_low for c in prior)

        if bid > band_high:
            direction = "BUY"
        elif bid < band_low:
            direction = "SELL"
        else:
            return None

        # Gate 3 — projection consensus: the day's bid curve must, across the
        # weighted models, project in the breakout direction strongly enough.
        result = consensus(
            buf.bid_closes,
            direction=direction,
            horizon=self.projection_horizon,
            weights=self.projection_weights,
            reference=bid,
            degree=self.projection_degree,
            ema_span=self.projection_ema_span,
        )
        if result.score < self.min_projection_score:
            logger.debug(
                "Donchian-projection %s on %s rejected: score=%.2f < %.2f "
                "(%d/%d models agree)",
                direction,
                epic,
                result.score,
                self.min_projection_score,
                result.agree,
                result.active,
            )
            return None

        logger.debug(
            "Donchian-projection %s on %s: bid=%.5f band=[%.5f, %.5f] ER=%.2f "
            "score=%.2f (%d/%d models agree)",
            direction,
            epic,
            bid,
            band_low,
            band_high,
            er,
            result.score,
            result.agree,
            result.active,
        )
        return EntryIntent(epic=epic, direction=direction, score=result.score)
