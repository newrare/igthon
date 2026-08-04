"""Cross-epic ranker — ``open_saferanking`` plus a hard *regime* veto.

:class:`~src.entry.open_saferanking.OpenSafeRanking` already asks every dimension
of a safe rise to hold at once (conjunctive geometric mean, pull-back safety,
multi-timeframe shape, a whole-day + recent up-trend veto). What it still cannot
refuse is a market that is **rising without going anywhere** — a directionless
range whose net drift happens to be positive. Its efficiency-ratio term is one
*soft* component out of six (``weight_regime = 0.10``), and because a ranker must
crown the best of the pool, a soft penalty never rejects: the least-bad chopping
market still opens.

This ranker adds exactly one thing: a **hard veto on the regime**. An epic whose
path over the last ``regime_period`` candles is not directional is dropped
outright, before any scoring.

Everything else — the components, their weights, the projection breadth gate, the
trend gate, the bearish malus, the wallet-bounded rolling selection — is inherited
unchanged from ``open_saferanking``. This is a subclass rather than a copy on
purpose: the two strategies differ by one rule, and duplicating the scoring
machinery would leave two versions of it to keep in step.

Why the regime deserves a veto rather than a weight
---------------------------------------------------
The Efficiency Ratio is ``|net move| / path travelled`` over the window: ``1`` for
a clean ramp, ``0`` for pure chop. It measures something no other component does —
not *whether* the curve rose, but whether it **went** anywhere to get there.

Observed on ``IX.D.HANGSENG.IFD.IP`` (2026-08-03 07:24, ER = 0.00 over the hour
before the open): the price travelled **135 points of path for a net move of 0.3
point**, oscillating inside a 38.5-point band and finishing where it started. Every
other dimension can look acceptable on such a curve — there is a fitted slope, a
projection, a spread — yet the trade has no thesis to be right about. It lost the
full initial risk after 5 h 20 whatever stop it was given.

That is also why the fix belongs here and not in :mod:`src.stops`: no stop
placement rescues a trade taken without direction. A wider stop only buys a larger
loss, a tighter one only reaches it sooner. The decision that matters is *not
opening*.

Measured effect, and how far to trust it
----------------------------------------
Replayed over the 153 positions of 2026-07-27 → 2026-08-03 (the window where the
``candle`` table still holds the pre-open history), filtering on the efficiency
ratio of the 60 candles before each open:

===============  ======  ==========  ==============
threshold        trades  realised    winning days
===============  ======  ==========  ==============
none              153     −293 €      2 / 6
ER ≥ 0.15          97     +592 €      3 / 6
ER ≥ 0.20          71    +1419 €      4 / 6
ER ≥ 0.25          40     −301 €      3 / 6
===============  ======  ==========  ==============

The effect is real — it holds on five of the six days rather than resting on one
lucky trade. The *exact* threshold is not: the collapse at 0.25 shows this is no
clean "more direction is better" monotone, so the apparent optimum at 0.20 is
fitted to 153 samples. ``min_regime_efficiency`` therefore defaults to the more
conservative **0.15** (broader plateau, destroys less volume), and it is a constant
to re-fit with :mod:`src.backtest.backtester` over more history, not a tuned value.

Two further caveats on the table above. It is *filter arithmetic*, not a replay:
refusing an open frees the epic, so with ``ALLOW_SAME_DAY_REOPEN=false`` the real
day would have taken different trades afterwards, and ``ALLOW_RECOVERY_REVERT``
interacts too. And the window is six days.

Relation to ``stop_shape``
--------------------------
:class:`~src.stops.stop_shape.StopShape` classifies the same curve with the same
measure over the same 60-candle window, to choose *which* recent level its stop
anchors on. Composing the two makes the pair coherent: this ranker refuses the
chop opens, so the stop policy's chop branch (its widest, least informative
placement) becomes the rare fallback it is meant to be rather than a routine case.
Keep ``min_regime_efficiency`` and ``StopShape.min_efficiency`` in step — they are
the same judgement about the same window.

Documented in ``docs/strategies/open_ultraranking.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.core.indicators import efficiency_ratio
from src.entry.base import EntryIntent
from src.entry.open_saferanking import OpenSafeRanking
from src.feed.price_buffer import EpicBuffer

logger = logging.getLogger(__name__)


@dataclass
class OpenUltraRanking(OpenSafeRanking):
    """Conjunctive ranker that refuses to rank a directionless market at all."""

    name = "open_ultraranking"

    # Hard regime veto. Deliberately a *different* window from the inherited soft
    # ``efficiency_period`` (30 candles, weight 0.10): the soft term ranks among
    # survivors on a short horizon, while this one asks the coarser question "has
    # this market gone anywhere at all in the last hour?" — the horizon on which
    # chop is actually identifiable, and the same window
    # :class:`~src.stops.stop_shape.StopShape` classifies on.
    regime_period: int = 60  # candles (~1 h) of mid closes measured
    # Floor under which the path counts as directionless and the epic is dropped.
    # Fitted on six days only — see the module docstring before moving it.
    min_regime_efficiency: float = 0.15

    @property
    def warmup(self) -> int:
        # ``efficiency_ratio`` consumes ``period + 1`` values, so the veto needs
        # one candle more than its window on top of whatever the base requires.
        return max(super().warmup, self.regime_period + 1)

    @classmethod
    def from_settings(cls, settings) -> OpenUltraRanking:
        # All parameters are constants of this class and of its base (the dataclass
        # field defaults), so the strategy builds from those and ignores
        # ``settings``. Tune by editing the constants; select it via OPEN_STRATEGY.
        return cls()

    def evaluate(self, epic: str, buf: EpicBuffer) -> EntryIntent | None:
        """Veto a directionless market, then defer to the inherited ranking.

        The veto runs *first* and on its own data, so a chopping epic costs one
        efficiency-ratio pass instead of the full projection consensus — this runs
        over every tradable epic on every selection pass.
        """
        if len(buf) < self.warmup:
            return None  # not enough history to judge the regime

        regime = efficiency_ratio(buf.mid_closes, self.regime_period)
        if regime < self.min_regime_efficiency:
            logger.debug(
                "UltraRanking %s rejected: directionless (ER %.3f < %.3f over %d)",
                epic,
                regime,
                self.min_regime_efficiency,
                self.regime_period,
            )
            return None

        return super().evaluate(epic, buf)
