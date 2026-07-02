"""Zone 2 updater — the bid is in the noise band just above break-even.

The bid sits above break-even (``level_zero``) but has not yet cleared the margin
level (``level_zero + noise_margin``). This is the delicate region: raising the
stop here would park it a hair above break-even, where ordinary bid/offer noise
alone would trigger it for ~zero profit (the "everything exits at 0 €" pathology).

Current behaviour: **hold the initial stop untouched** — the stop only ever moves
once the bid has cleared the margin level (zone 3). Kept as its own updater so a
future, carefully-bounded near-break-even ratchet can live here without touching
the underwater or profit-trailing zones.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.exit.zones.base import StopContext, StopUpdater


@dataclass
class BreakevenBandStop(StopUpdater):
    """Hold the stop while the bid is in the noise band above break-even."""

    name = "hold"

    def propose(self, ctx: StopContext) -> float | None:
        return None
