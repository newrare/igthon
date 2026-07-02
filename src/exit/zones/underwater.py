"""Zone 1 updater — the bid is at or below break-even.

Current behaviour: **hold the initial protective stop untouched**. The stop
posted at open is never lowered and never nudged while the trade is underwater;
it is left to the broker to fill it if price falls that far. This preserves the
profit-gated profile's rule of not touching the stop until the trade is genuinely
in profit.

This is deliberately a clean extension point: an underwater-management scenario
(e.g. steering the stop by the trend since open) would live here without
touching the break-even-band or profit-trailing zones.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.exit.zones.base import StopContext, StopUpdater


@dataclass
class UnderwaterStop(StopUpdater):
    """Hold the initial stop while the bid is at or below break-even."""

    name = "hold"

    def propose(self, ctx: StopContext) -> float | None:
        return None
