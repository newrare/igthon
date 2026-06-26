"""Donchian breakout entry, gated by the Kaufman Efficiency Ratio.

This is the *open* side only: it decides **whether and which way to enter**,
and emits an :class:`~src.entry.base.EntryIntent` with the direction. It says
nothing about the stop, the target or the trailing — those belong to the
:class:`~src.exit.base.CloseProfile` composed with it at runtime (the reference
pairing is :class:`~src.exit.atr_trailing.AtrTrailingExit`).

Entry: the bid closes outside the prior ``channel``-period high/low band
(Donchian channel) → enter the breakout in that direction.

Quality gates applied **before** any entry, in order:

1. **Spread gate** — ``spread / bid`` must stay under ``max_spread_ratio``.
2. **Regime gate** — the Kaufman Efficiency Ratio over ``efficiency_period``
   candles must reach ``min_efficiency`` (ER ≈ 1 clean trend, ≈ 0 chop).

The strategy emits both BUY and SELL intents; the live pipeline currently
opens BUY only (the risk gate rejects SELL), so short breakouts are ignored
until short support lands in the execution layer.

Documented in ``docs/strategies/donchian-er.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.core.indicators import atr, efficiency_ratio
from src.entry.base import EntryIntent, EntryStrategy
from src.feed.price_buffer import EpicBuffer

logger = logging.getLogger(__name__)


@dataclass
class DonchianEntry(EntryStrategy):
    """N-period channel breakout, armed only when the market is trending."""

    name = "donchian_er"

    channel: int = 20  # Donchian lookback (prior candles forming the band)
    atr_period: int = 14  # only used to confirm there is measurable volatility
    efficiency_period: int = 30  # ER lookback window
    min_efficiency: float = 0.60  # regime gate threshold (0 disables)
    max_spread_ratio: float = 0.0010  # tightened: a breakout edge dies on spread

    @property
    def warmup(self) -> int:
        return max(self.channel, self.efficiency_period, self.atr_period) + 1

    @classmethod
    def from_settings(cls, settings) -> DonchianEntry:
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

        logger.debug(
            "Donchian %s on %s: bid=%.5f band=[%.5f, %.5f] ER=%.2f",
            direction,
            epic,
            bid,
            band_low,
            band_high,
            er,
        )
        return EntryIntent(epic=epic, direction=direction, score=er)
