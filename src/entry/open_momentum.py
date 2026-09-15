"""Open the *start* of an up leg, and only while the book has nothing running.

Two readings of the same curve have to agree before this entry opens anything,
and they deliberately answer different questions
(:mod:`src.core.reading`):

* :attr:`~src.core.reading.CurveReading.regime` — **where** the market is:
  ``TREND_UP`` means the path over the last ``context_minutes`` is directional
  (efficiency ratio past the threshold) *and* the fitted trend points up. It says
  nothing about the age of the move: it reads the same on the first candle of a
  rise and on its last.
* :attr:`~src.core.reading.CurveReading.move_phase` — **how far along** the move
  is: ``START`` means the 9/21 crossing carrying the current leg is recent (not
  yet established). It says nothing about whether the surrounding market is
  going anywhere.

Asking for both is the whole idea: ``TREND_UP`` alone buys established rises,
which is where the give-back lives; ``START`` alone buys every fresh crossing,
most of which are chop (that is what the regime filters out). Together they read
"a market that *is* going somewhere, caught early".

The direction of the leg is checked separately
(:attr:`~src.core.reading.CurveReading.move_direction` must be ``UP``): a fresh
*down* crossing inside a market whose 30-minute regime still reads ``TREND_UP``
is a leg turning against the trend, not the start of a rise.

One bet at a time, and only into a quiet book
---------------------------------------------
This entry is a cross-epic ranker, but it is not the usual "hold N positions"
kind. The rule it implements is a rule about the **book**, not about a count:

* it opens **one position per selection pass** (``open_cooldown_minutes = 1``,
  which caps the pass at a single open and spaces two opens by a minute), so a
  basket is never fired at once and every open is decided against the book as it
  actually is;
* it opens nothing while **any** open position sits in a market that is still
  trending (``require_range_book``, enforced by the scheduler, which is the layer
  that can see the open positions). Only when *every* open position's epic reads
  ``RANGE`` — the move it was opened for is over, the market is chopping inside a
  contained band — may a new momentum trade go on.

That brake is what "one at a time" means here. A position that is still riding
its move must not be diluted by a second bet; a position whose market has gone
back to a range is no longer using the attention, and a fresh start elsewhere is
worth more than sitting on it. Anything that is neither (``VOLATILE``, or an epic
with too little history to judge) blocks too: the brake exists to open only into
a book that is *demonstrably* quiet, never into an unknown one.

Because the count target is dropped (``wallet_bounded``), the book grows one
position at a time exactly as long as the range condition keeps holding, and the
wallet is the only other limit.

Ranking
-------
Every gate here is absolute — an epic either shows a fresh up leg inside an
up regime or it does not — so the score only orders candidates that have already
qualified. It is the efficiency ratio of the context window: among two markets
starting a leg, prefer the one whose path actually travels rather than saws. For
the same reason the participation gates are off: this is not a tournament where
the least-bad of a small pool wins, so a thin universe is not a reason to refuse
a qualifying setup.

Documented in ``docs/strategies/open_momentum.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.core.reading import CurveReading, MovePhase, Regime, Trend
from src.entry.base import EntryIntent, EntryStrategy
from src.feed.price_buffer import EpicBuffer

logger = logging.getLogger(__name__)


@dataclass
class OpenMomentum(EntryStrategy):
    """Buy the start of an up leg inside an up regime, one bet at a time."""

    name = "open_momentum"

    # Cross-epic ranker: the two rules above are decided against the *book*, which
    # only the rolling selector can see, so this entry cannot be a per-epic one.
    cross_epic_selection = True
    emits_shorts = False
    # No fixed position count: how many positions may be open is decided by the
    # range-book brake below, not by a number. ``concurrent_positions`` is only the
    # conservative fallback the selector uses for a pass where the account balance
    # cannot be read.
    wallet_bounded = True
    concurrent_positions = 1
    # Open nothing while any open position's market is still trending; a book
    # entirely in ``RANGE`` re-opens the door. See the module docstring.
    require_range_book = True
    book_regime_window = 30
    # One open per pass, and at least a minute between two of them: each open is
    # then judged against a book that already includes the previous one.
    open_cooldown_minutes = 1
    # No tournament, no participation gate — the gates are absolute (see above).
    min_participation_ratio = 0.0
    min_participation_count = 0

    # ---- horizons --------------------------------------------------------------
    # Window the regime, the give-back and the ranking efficiency are judged on.
    # Kept equal to ``book_regime_window`` so the entry and the brake speak about
    # the same stretch of curve.
    context_minutes: int = 30
    # Candles required before an epic is scored. 60 gives the 9/21 crossing room to
    # be measured as *recent* rather than merely unmeasurable, and gives
    # ``volatility_ratio`` a baseline longer than the window it compares to.
    warmup_minutes: int = 60

    @property
    def warmup(self) -> int:
        return self.warmup_minutes

    @classmethod
    def from_settings(cls, settings) -> OpenMomentum:
        # Every parameter is a constant of this class (the dataclass field
        # defaults), so the strategy builds from those and ignores ``settings``.
        # Tune by editing the constants; select it with OPEN_STRATEGY.
        return cls()

    def evaluate(self, epic: str, buf: EpicBuffer) -> EntryIntent | None:
        """Emit a BUY when the market starts an up leg inside an up regime.

        Returns ``None`` on structural grounds (too little history, no price, no
        measurable volatility) and whenever either reading disagrees.
        """
        if len(buf) < self.warmup:
            return None

        reading = CurveReading.of(buf, regime_window=self.context_minutes)
        if reading.price <= 0 or reading.atr <= 0:
            return None

        if reading.regime is not Regime.TREND_UP:
            return None
        if reading.move_phase is not MovePhase.START:
            return None
        if reading.move_direction is not Trend.UP:
            return None

        # Ranking only — every acceptance decision is already made. 0-1, 1 being a
        # straight line over the context window.
        score = max(0.0, min(1.0, reading.amplitude(self.context_minutes).efficiency))
        logger.debug(
            "Momentum %s: START of an up leg in a TREND_UP regime (ER %.3f)",
            epic,
            score,
        )
        return EntryIntent(epic=epic, direction="BUY", score=score)
