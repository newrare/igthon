"""Testing entry — open as many *different* markets per day as the wallet allows.

This is a diagnostic entry strategy, not a money-making one. Its sole purpose is
to exercise the *close* side of the pipeline — the protective stop
(:mod:`src.stops`) and the three close zones (:mod:`src.exit.zones`) — across a
wide, varied set of live positions in a single day, so their behaviour can be
observed on real markets rather than a single rolling trade.

Like :class:`~src.entry.open_ranking.OpenRanking` it is a **cross-epic ranker**
(``cross_epic_selection = True``), so it plugs straight into the scheduler's
rolling-selection routine (``_select_and_open``) and reuses, unchanged, every
guarantee that routine already provides:

* **One opening per epic per day** — an epic already used today is dropped from
  the candidate set (``_traded_today_epics``), so the bot keeps spreading across
  *new* markets instead of re-opening the same one.
* **Open until the wallet is exhausted** — the selector opens ranked candidates
  one by one, subtracting each epic's margin from the spendable balance
  (available funds minus :attr:`wallet_reserve`) and stopping when the next
  margin no longer fits. A very large :attr:`concurrent_positions` target means
  the per-tick slot count is effectively unbounded, so the wallet — not an
  arbitrary position cap — is what limits how many markets open.
* **Diversified, random selection** — :meth:`evaluate` returns a *random* score
  in ``[0, 1)`` for every scorable epic, so the ranking (and therefore the order
  in which the wallet is spent) is shuffled every tick. Which markets get the
  last few affordable slots varies day to day, giving broad, non-deterministic
  coverage of the tradable universe rather than always the same alphabetical or
  highest-trend few. The tradable universe itself is already balanced across
  asset classes by the scheduler's ``select_diversified_subset``.

It stays true to the open/close decoupling: :meth:`evaluate` emits only a
direction (BUY — the live risk gate is long-only, see
:func:`src.execution.gates.evaluate_open_gates`) and a score. It says nothing
about the stop/target/trailing, which belong to the composed
:class:`~src.exit.base.CloseProfile` and the selected
:class:`~src.stops.base.StopDistance` — precisely the machinery this profile
exists to stress-test.

The only structural requirement is a positive ATR (measurable volatility): with
no volatility the composed stop distance cannot size a protective stop at open,
so such an epic is skipped rather than opened blind.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from src.core.indicators import atr
from src.entry.base import EntryIntent, EntryStrategy
from src.feed.price_buffer import EpicBuffer

logger = logging.getLogger(__name__)


@dataclass
class OpenTesting(EntryStrategy):
    """Randomly open a maximum of different markets/day to test stops + zones."""

    name = "open_testing"
    cross_epic_selection = True

    # Rolling-selection constants (read by the scheduler). Plain class attributes
    # — not settings — so they stay constants of the strategy.
    #
    # A deliberately huge target: the scheduler opens up to ``target - open_count``
    # markets per tick, so making the target far larger than the tradable universe
    # means the *wallet* (available funds minus ``wallet_reserve``) is the only
    # real limit on how many different epics open — exactly the "open as many as
    # the wallet allows" goal.
    concurrent_positions = 1000
    open_after_minutes = 0  # no wall-clock warm-up: open as soon as epics warm up
    wallet_reserve = 0.05  # keep a small buffer free to avoid a margin call
    # Open as soon as *any* epic is warmed up — a testing profile wants to start
    # spreading across markets immediately, not wait for half the universe.
    min_participation_ratio = 0.0

    # Minimum buffered candles before an epic can be opened. Small on purpose so
    # opens start early in the session, but ≥ ``atr_period + 1`` so a meaningful
    # ATR (and therefore a sized protective stop) is always available. The
    # composed stop distance degrades gracefully with a short window (it slices
    # its own lookback and has ATR/spread floors), so a modest warm-up still
    # yields a valid stop to observe.
    atr_period: int = 14
    warmup_candles: int = 20

    @property
    def warmup(self) -> int:
        # Never let the warm-up drop below what ATR needs to be non-zero.
        return max(self.warmup_candles, self.atr_period + 1)

    @classmethod
    def from_settings(cls, settings) -> OpenTesting:
        # All parameters are constants of this class; the strategy builds from its
        # own defaults and ignores ``settings``. Select it with OPEN_STRATEGY.
        return cls()

    def evaluate(self, epic: str, buf: EpicBuffer) -> EntryIntent | None:
        candles = list(buf.candles)
        if len(candles) < self.warmup:
            return None  # not enough history to size a stop
        last = candles[-1]
        if last.bid_close <= 0:
            return None

        # Structural requirement: a positive ATR, so the composed close profile
        # can size a protective stop at open. No volatility -> skip (never open
        # a position whose stop cannot be placed).
        if atr(candles, self.atr_period) <= 0:
            return None

        # Random score -> the scheduler's ranking is shuffled every tick, so the
        # order in which the wallet is spent (and thus which markets get the last
        # affordable slots) varies. This is the "random but diversified" opening.
        score = random.random()

        logger.debug("OpenTesting %s: random score=%.3f", epic, score)
        return EntryIntent(epic=epic, direction="BUY", score=score)
