"""Cross-epic ranker — open the markets whose day is a clean, rising straight line.

Like :class:`~src.entry.open_ranking.OpenRanking`,
:class:`~src.entry.open_saferanking.OpenSafeRanking`,
:class:`~src.entry.open_allincrease.OpenAllIncrease`,
:class:`~src.entry.open_rebound.OpenRebound` and
:class:`~src.entry.open_slope.OpenSlope` this is a **ranker**, not a gate
(``cross_epic_selection = True``): the scheduler scores every tradable epic,
ranks the BUY candidates and opens the best affordable ones. This module owns
only the *per-epic* half — "how closely does the whole day trace a rising
straight line?" — and stays exit-agnostic (:meth:`evaluate` emits an
:class:`~src.entry.base.EntryIntent` carrying only the BUY direction and a
comparable opening score in [0, 1]; the stop/target/trailing belong to the
composed :class:`~src.exit.base.CloseProfile`).

The setup this ranker looks for
-------------------------------

The shape a trader recognises at a glance and opens on manually (the spec,
translated): *the day's general trend is bullish and the curve has been roughly
linear since the morning* — a steady, ruler-straight climb, not a choppy grind
and not a fresh spike. This is the pure trend-following counterpart to
:class:`~src.entry.open_rebound.OpenRebound` (which wants a V) and to
:class:`~src.entry.open_slope.OpenSlope` (which looks only at the last ~10 min):
here the whole buffered session — "la journée" — must itself be a rising line.

Two independent measures of *straightness* over the whole session are combined,
because they penalise different defects and a clean line scores high on both:

1. **Linearity (R²).** The least-squares fit of the day-long bid regression.
   R² is high when points sit close to the fitted line — it tolerates a little
   noise as long as the *shape* is a line, and collapses when the curve bends
   (an accelerating parabola or a late roll-over both fit a straight line badly).
2. **Efficiency (Kaufman ER).** ``|net move| / Σ|step move|`` over the session:
   1.0 for a monotonic march, near 0 for a path that wanders up and down to get
   nowhere. It penalises *choppiness* directly, independent of the fitted line.

A day can score well on one and poorly on the other (a smooth parabola has high
ER but mediocre R²; a straight line drawn through a saw-tooth has decent R² but
low ER), so rewarding both is what pins the score to a genuinely *linear* climb.

Scoring — bullish + linear + not flat
-------------------------------------

```
day_reg  = linear_regression(bids)          # whole session ("since the morning")
if day_reg.slope <= 0:  → None               # not bullish over the day → stay flat
linearity  = clamp01(day_reg.r_squared)      # straight-line fit
efficiency = efficiency_ratio(bids, N-1)     # path directness (choppiness guard)
net_rise   = day_reg.slope · (N − 1)         # fitted rise over the session (points)
strength   = clamp01((net_rise / bid) / rise_target)       # relative progression
score = w_lin·linearity + w_eff·efficiency + w_str·strength
```

The **strength** term is the deliberate guard against a *flat* line: a curve can
be perfectly straight and perfectly efficient while barely rising, which is not a
tradable up-day. Expressing the net rise as a fraction of the current bid keeps
the term comparable across epics of any price scale (an index moving in whole
points and a forex pair in ten-thousandths saturate at the same relative climb).
An ATR-relative measure would *not* work: for a clean line the per-candle ATR
scales with the slope, so ``net_rise / ATR`` is invariant to steepness and could
never tell a flat line from a steep one — only a price-relative progression can.

The composite is a **weighted sum** (weights sum to 1.0, so the score stays in
[0, 1] and is directly comparable across epics and readable as a percentage). The
two straightness terms carry the majority — this ranker is about the *shape* —
with strength as the qualifying floor. :meth:`evaluate` returns ``None`` on
*structural* grounds (too little history, non-positive bid, no measurable
volatility — ``ATR ≤ 0`` would leave the close profile unable to size a stop),
when the day is not rising (long-only), or below the optional ``min_score`` floor.

Selection-layer behaviour (spec)
--------------------------------

The class attributes read by the scheduler's rolling selector mirror the sibling
rankers:

- ``wallet_bounded = True`` — keep opening the best-ranked affordable clean
  up-day until the spendable balance (available funds minus ``wallet_reserve``)
  can no longer cover another margin.
- ``open_cooldown_minutes = 5`` — the selector opens at most one position per
  pass and waits at least five minutes between two opens, so several markets
  trending together are not opened in a single burst.
- ``allow_same_day_reopen = False`` — each epic may be opened at most once per
  day. On top of the shared ``epic_already_open`` gate (which blocks concurrent
  duplicates), the ``_traded_today`` diversity filter drops any epic already
  opened today from re-ranking, so a market that has been used is not re-opened
  even after its previous position has closed. The rolling selector therefore
  rotates across markets rather than re-opening the same linear climber.

Documented in ``docs/strategies/open_linear.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.core.indicators import atr, efficiency_ratio, linear_regression
from src.entry.base import EntryIntent, EntryStrategy
from src.feed.price_buffer import EpicBuffer

logger = logging.getLogger(__name__)


def _clamp01(value: float) -> float:
    """Clamp ``value`` to the closed unit interval [0, 1]."""
    return max(0.0, min(1.0, value))


@dataclass
class OpenLinear(EntryStrategy):
    """Rank markets by how cleanly the whole day traces a rising straight line."""

    name = "open_linear"
    cross_epic_selection = True

    # Rolling-selection constants (read by the scheduler). Plain class attributes
    # — not dataclass fields, not settings — so they stay constants of the
    # strategy. The strategy is selected at runtime via ``OPEN_STRATEGY``.
    #
    # Wallet-bounded, paced and one-open-per-day: keep opening the best-ranked
    # affordable clean up-day — but each epic at most once per day — one at a time,
    # at least ``open_cooldown_minutes`` apart, until the spendable balance
    # (available funds minus ``wallet_reserve``) can no longer cover another
    # margin. Once an epic has been opened today it is dropped from re-ranking
    # (the shared ``_traded_today`` diversity filter), so it is not re-opened even
    # after its position has closed.
    wallet_bounded = True  # open epics as long as the wallet has funds
    concurrent_positions = 1  # fallback cap only, used when the balance is unknown
    allow_same_day_reopen = False  # one opening per epic per day — no re-open once used
    open_cooldown_minutes = 5  # ≥5 min between two opens; one open per pass
    open_after_minutes = 60  # ≈ one hour of livestream warm-up before first open
    wallet_reserve = 0.10  # keep 10% of available funds free
    min_participation_ratio = 0.5  # > half the warmed-up universe before crowning

    # Windows (candles ≈ minutes on the one-minute feed). The linear day-trend is
    # measured over the *whole buffered session* (the "journée depuis le matin"),
    # so it is not a tunable window; ``min_period`` is only the minimum amount of
    # session that must have accumulated before straightness is worth judging (a
    # handful of candles trivially fit a line) and ``atr_period`` sizes volatility.
    min_period: int = 30  # ~30 min of session required before scoring straightness
    atr_period: int = 14  # volatility window (also gates stop sizing at open)

    # Magnitude scale: the fitted net rise over the session, as a fraction of the
    # current bid (a relative progression, comparable across price scales), at
    # which the strength factor saturates to 1. A straight but nearly *flat* line
    # rises only a sliver over the day and earns a low factor however clean it
    # looks. An ATR-relative measure would *not* work here: for a clean line the
    # per-candle ATR scales with the slope, so net_rise/ATR is invariant to
    # steepness and could never tell a flat line from a steep one — only a
    # price-relative progression detects flatness. ``0.01`` = a 1 % session rise
    # earns full marks.
    rise_target: float = 0.01

    # Composite weights — a weighted sum summing to 1.0 so the score stays in
    # [0, 1] / readable as a percentage. The two straightness terms carry the
    # majority (this ranker is about the linear *shape*); strength is the
    # anti-flat qualifier rather than the driver.
    weight_linearity: float = 0.45  # R² of the day-long fit (straight-line shape)
    weight_efficiency: float = 0.30  # Kaufman ER (directness / choppiness guard)
    weight_strength: float = 0.25  # net rise vs own volatility (anti-flat)

    # Composite floor: below this the epic stays flat (0.0 = never floor / pure
    # ranking). Set to 0.60 — the point that separates genuinely linear rising
    # days (trend_up open-tick scores: p10≈0.61, median≈0.81) from volatile,
    # hump-shaped noise (median≈0.05): it keeps ~91% of clean up-days while
    # rejecting ~3/4 of volatile days. Because it gates every tick, the strategy
    # no longer opens on an early choppy stretch — it waits for a stretch that is
    # actually a straight climb before opening. Raise it for even cleaner lines.
    min_score: float = 0.60

    @property
    def warmup(self) -> int:
        # Bounded by the longer of the minimum session and the ATR window (+1 for
        # the true-range differencing). The day-long regression and efficiency
        # ratio then use whatever history has accumulated beyond that.
        return max(self.min_period, self.atr_period) + 1

    @classmethod
    def from_settings(cls, settings) -> OpenLinear:
        # All parameters are constants of this class (the dataclass field defaults
        # above), so the strategy builds from those and ignores ``settings``. Tune
        # by editing the constants here; select it at runtime via ``OPEN_STRATEGY``.
        return cls()

    def evaluate(self, epic: str, buf: EpicBuffer) -> EntryIntent | None:
        candles = list(buf.candles)
        if len(candles) < self.warmup:
            return None  # not enough of a session to judge the day's shape
        last = candles[-1]
        bid = last.bid_close
        if bid <= 0:
            return None

        # A positive ATR is required structurally: without volatility the composed
        # close profile cannot size a protective stop at open, and it is the unit
        # in which the rise strength is measured.
        atr_value = atr(candles, self.atr_period)
        if atr_value <= 0:
            return None

        bids = buf.bid_closes

        # Gate — bullish day. The whole-session slope must be strictly positive:
        # a market falling (or flat) over the day is not a rising line. Long-only,
        # so refuse it outright rather than ranking it lower.
        day_reg = linear_regression(bids)
        if day_reg.slope <= 0:
            logger.debug(
                "Linear %s rejected: day trend not rising (slope %.5g)",
                epic,
                day_reg.slope,
            )
            return None

        # 1. Linearity — how well the day fits a straight line (R²).
        linearity = _clamp01(day_reg.r_squared)

        # 2. Efficiency — how directly the path travels its net distance (Kaufman
        #    ER over the whole session), penalising choppiness the R² fit tolerates.
        efficiency = efficiency_ratio(bids, len(bids) - 1)

        # 3. Strength — fitted net rise over the session as a fraction of the
        #    current bid (a relative progression), so a straight-but-flat line
        #    scores low. Saturates at ``rise_target``.
        net_rise = day_reg.slope * (len(bids) - 1)
        strength = _clamp01(
            (net_rise / bid) / self.rise_target if self.rise_target > 0 else 0.0
        )

        score = (
            self.weight_linearity * linearity
            + self.weight_efficiency * efficiency
            + self.weight_strength * strength
        )

        if score < self.min_score:
            logger.debug(
                "Linear %s below floor: score=%.3f < %.2f "
                "(linearity=%.2f efficiency=%.2f strength=%.2f)",
                epic,
                score,
                self.min_score,
                linearity,
                efficiency,
                strength,
            )
            return None

        logger.debug(
            "Linear %s: score=%.3f (linearity=%.2f efficiency=%.2f strength=%.2f "
            "slope=%.6g net_rise=%.6g)",
            epic,
            score,
            linearity,
            efficiency,
            strength,
            day_reg.slope,
            net_rise,
        )
        return EntryIntent(epic=epic, direction="BUY", score=score)
