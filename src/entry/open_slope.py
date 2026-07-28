"""Cross-epic ranker — open the markets rising fastest right now (recent slope).

Like :class:`~src.entry.open_ranking.OpenRanking`,
:class:`~src.entry.open_saferanking.OpenSafeRanking`,
:class:`~src.entry.open_allincrease.OpenAllIncrease` and
:class:`~src.entry.open_rebound.OpenRebound` this is a **ranker**, not a gate
(``cross_epic_selection = True``): the scheduler scores every tradable epic,
ranks the BUY candidates and opens the best affordable ones. This module owns
only the *per-epic* half — "how steeply is this curve rising over the last few
minutes?" — and stays exit-agnostic (:meth:`evaluate` emits an
:class:`~src.entry.base.EntryIntent` carrying only the BUY direction and a
comparable opening score; the stop/target/trailing belong to the composed
:class:`~src.exit.base.CloseProfile`).

The spec, translated
---------------------

*Compute the slope of the recent trend (over ~10 minutes), then rank the
available (livestreamed) epics, placing first the one with the highest slope /
progression. Keep opening as long as the wallet allows. An epic that is currently
open cannot be opened again. Open at most one new position every 5 minutes.*

This is the simplest of the rankers: a single measure — the **recent slope** —
drives the whole ranking. There is no multi-horizon blend, no shape/regime
tie-breaker; the fastest-rising market wins.

Scoring — recent progression
-----------------------------

The slope comes from a least-squares :func:`~src.core.indicators.linear_regression`
over the last ``slope_period`` bid closes (~10 min on the one-minute feed), which
is more robust to endpoint jitter than a two-point rate of change. A **raw** slope
is not comparable across epics — a €15 000 index moves in whole points while a
forex pair moves in ten-thousandths — so the slope is expressed as a **relative
progression**: the fitted net rise over the window divided by the current bid.

```
reg      = linear_regression(bids[-slope_period:])
net_rise = reg.slope · (slope_period − 1)   # fitted rise over the window (points)
score    = net_rise / bid                    # progression over the window (fraction)
```

The score is therefore the fraction by which the fitted line rose over the last
~10 minutes, directly comparable across epics of any price scale and readable as
a percentage. The ranker is **long-only**: a market whose recent slope is not
strictly positive is not a rising market, so :meth:`evaluate` returns ``None``
rather than ranking it (a falling curve is never opened). It also returns ``None``
on structural grounds (too little history, non-positive bid, no measurable
volatility — ``ATR ≤ 0`` would leave the close profile unable to size a stop) and
below the optional ``min_score`` floor (default 0 = never floor, so pure ranking).

Selection-layer behaviour (spec)
--------------------------------

Four class attributes read by the scheduler's rolling selector realise the rest
of the spec directly:

- ``wallet_bounded = True`` — *ouvrir tant que le wallet le permet*: no fixed
  position count, keep opening the best-ranked affordable rising epics until the
  spendable balance (available funds minus ``wallet_reserve``) can no longer cover
  another margin.
- ``open_cooldown_minutes = 5`` — *une nouvelle ouverture toutes les 5 minutes au
  mieux*: the selector opens at most one position per pass and waits at least five
  minutes since the most recent open before opening the next.
- ``ALLOW_SAME_DAY_REOPEN=true`` (global ``.env`` policy, no longer a strategy
  attribute) — the only re-open restriction in the spec is
  *un epic actuellement ouvert ne pourra pas être ouvert de nouveau*, which is the
  shared ``epic_already_open`` gate (always on, blocks concurrent duplicates).
  Nothing forbids re-opening an epic once it has closed, so the one-open-per-epic-
  per-day diversity filter is disabled: a market that is flat again is a candidate
  again, and the fastest riser can be re-opened.

Documented in ``docs/strategies/open_slope.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.core.indicators import atr, linear_regression
from src.entry.base import EntryIntent, EntryStrategy
from src.feed.price_buffer import EpicBuffer

logger = logging.getLogger(__name__)


@dataclass
class OpenSlope(EntryStrategy):
    """Rank markets by their recent (~10 min) slope; open the steepest risers."""

    name = "open_slope"
    cross_epic_selection = True

    # Rolling-selection constants (read by the scheduler). Plain class attributes
    # — not dataclass fields, not settings — so they stay constants of the
    # strategy. The strategy is selected at runtime via ``OPEN_STRATEGY``.
    #
    # Wallet-bounded, paced and re-openable: keep opening the best-ranked
    # affordable rising market — the same epic may recur once it is flat again —
    # one at a time, at least ``open_cooldown_minutes`` apart, until the spendable
    # balance (available funds minus ``wallet_reserve``) can no longer cover
    # another margin. A concurrent duplicate on a still-open epic is always blocked
    # by the shared ``epic_already_open`` gate; re-opening a *flat* epic the same
    # day is the global ALLOW_SAME_DAY_REOPEN policy (.env) — this strategy
    # assumes true.
    wallet_bounded = True  # open epics as long as the wallet has funds
    concurrent_positions = 1  # fallback cap only, used when the balance is unknown
    open_cooldown_minutes = 5  # ≥5 min between two opens; one open per pass
    open_after_minutes = 60  # ≈ one hour of livestream warm-up before first open
    wallet_reserve = 0.10  # keep 10% of available funds free
    min_participation_ratio = 0.5  # > half the warmed-up universe before crowning

    # Windows (candles ≈ minutes on the one-minute feed).
    slope_period: int = 10  # ~10 minutes — the recent trend the slope is fitted on
    atr_period: int = 14  # volatility window (gates stop sizing at open)

    # Composite floor: below this the epic stays flat (0.0 = never floor / pure
    # ranking). The score is a relative progression, so a value like 0.001 would
    # require a ≥0.1 % rise over the window to qualify.
    min_score: float = 0.0

    @property
    def warmup(self) -> int:
        # Need the ATR window (the longer of the two) plus one candle for the
        # true-range differencing; the slope then fits on the last ``slope_period``.
        return max(self.slope_period, self.atr_period) + 1

    @classmethod
    def from_settings(cls, settings) -> OpenSlope:
        # All parameters are constants of this class (the dataclass field defaults
        # above), so the strategy builds from those and ignores ``settings``. Tune
        # by editing the constants here; select it at runtime via ``OPEN_STRATEGY``.
        return cls()

    def evaluate(self, epic: str, buf: EpicBuffer) -> EntryIntent | None:
        candles = list(buf.candles)
        if len(candles) < self.warmup:
            return None  # not enough history to fit the recent slope
        last = candles[-1]
        bid = last.bid_close
        if bid <= 0:
            return None

        # A positive ATR is required structurally: without volatility the composed
        # close profile cannot size a protective stop at open.
        if atr(candles, self.atr_period) <= 0:
            return None

        bids = buf.bid_closes

        # Recent slope over ~10 minutes, expressed as a progression relative to the
        # current bid so it is comparable across epics of any price scale.
        reg = linear_regression(bids[-self.slope_period :])
        if reg.slope <= 0:
            return None  # not rising over the recent window — long-only, stay flat
        net_rise = reg.slope * (self.slope_period - 1)
        score = net_rise / bid

        if score < self.min_score:
            logger.debug(
                "Slope %s below floor: score=%.5f < %.5f (slope=%.6g)",
                epic,
                score,
                self.min_score,
                reg.slope,
            )
            return None

        logger.debug(
            "Slope %s: score=%.5f (slope=%.6g net_rise=%.6g bid=%.5g)",
            epic,
            score,
            reg.slope,
            net_rise,
            bid,
        )
        return EntryIntent(epic=epic, direction="BUY", score=score)
