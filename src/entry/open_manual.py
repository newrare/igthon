"""Manual entry — the bot never opens; the user opens from the dashboard.

This is the *open* side reduced to a no-op: :meth:`evaluate` always returns
``None``, so the analysis loop
(:meth:`~src.core.scheduler.BotScheduler._collect_and_analyze`) can run on its
normal schedule without ever opening a position on its own. Opening becomes a
purely manual act performed by the user through the dashboard BUY button
(``POST /api/positions/open/{epic}``), which bypasses the entry strategy
entirely (it hard-codes the direction) and drives the *same* open path — sizing,
risk gates, the composed :class:`~src.exit.base.CloseProfile` stop, IG
confirmation and the DB record — as an automatic open would.

Why an explicit strategy rather than "just don't enable the analysis job":

* The three strategy selections in ``.env`` are all **required** and validated
  at startup (:func:`~src.core.scheduler.validate_strategy_selection`), so
  ``OPEN_STRATEGY`` must name a registered entry. ``open_manual`` is the
  first-class way to say "no automatic opening" without leaving the auto path
  armed with some real strategy.
* The rest of the pipeline stays fully live: prices still stream, candles still
  record, the close profile still manages every open position's exit, and the
  monitor/sync jobs still run. Only the *open decision* is handed to the user.

It keeps the open/close decoupling intact: it emits nothing but the (absent)
open decision, and says nothing about the stop/target/trailing — those remain
owned by the composed :class:`~src.exit.base.CloseProfile` and the selected
:class:`~src.stops.base.StopDistance`, and apply to every manual open exactly as
they would to an automatic one.

Documented in ``docs/strategies/open_manual.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.entry.base import EntryIntent, EntryStrategy
from src.feed.price_buffer import EpicBuffer

logger = logging.getLogger(__name__)


@dataclass
class OpenManual(EntryStrategy):
    """No automatic opening — the user opens each position from the dashboard."""

    name = "open_manual"

    @property
    def warmup(self) -> int:
        # No signal is ever computed, so no history is required. Kept at 1 (rather
        # than 0) to satisfy the "at least one candle" contract other code assumes.
        return 1

    @classmethod
    def from_settings(cls, settings) -> OpenManual:
        # No parameters: the strategy is a pure no-op. Select it with OPEN_STRATEGY.
        return cls()

    def evaluate(self, epic: str, buf: EpicBuffer) -> EntryIntent | None:
        # Never open automatically. Opening is done by the user from the dashboard
        # (POST /api/positions/open/{epic}), which bypasses this strategy.
        return None
