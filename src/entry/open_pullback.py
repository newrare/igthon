"""Cross-epic ranker — join a clean trend on a **pull-back**, both directions.

Reverse-engineered from the twelve manual opens of the 2026-07-24 session. Each
entry was replayed against the stored one-minute candles and its indicator state
placed inside a baseline of 8 586 market points from the same day. The manual
entries were strikingly consistent — they were not noise:

==================================  ================  ====================
feature                             manual median     baseline percentile
==================================  ================  ====================
``trend_pct`` over 60 candles       +0.71 %           95th
``trend_pct`` over 30 candles       +0.40 %           94th
R² of the 60-candle fit             0.83              88th
ATR as % of price                   0.069 %           80th
channel position (60)               0.83              78th
distance below the 60-bar extreme   1.71 ATR          76th
``roc`` over 5 candles (BUY only)   −0.045 %          **16th**
==================================  ================  ====================

That is one recognisable gesture: *wait for a clean, strong hour-long trend on a
market that actually moves, let it pause, and join it below the extreme rather
than at it.* This module is that gesture, made symmetric so it applies to a
down-trend as well.

.. warning::

   **This entry measured below breakeven and is published for live evaluation,
   not because it was validated.**

   Replayed over 6 sessions and ~100 000 resolved outcomes (1.5 ATR stop /
   3.0 ATR target, so breakeven = 33.33 %; an unfiltered entry measured 33.63 %):

   ==================================  =========  ==============
   rule                                n          win rate
   ==================================  =========  ==============
   unfiltered baseline                 99 732     33.63 %
   this rule (trend-pull-back)         794        **29.60 %**
   trend-continuation variant          1 317      30.98 %
   ==================================  =========  ==============

   The decile scan is what makes this hard to dismiss as a threshold artefact:
   *every* axis of the signature points into a losing bucket — ``slope60``
   decile 10 scores 32.4 % against 35.0 % for decile 1, ``chan60`` 31.5 % against
   35.4 %, ``brk60`` 31.9 % against 35.1 %. On this universe and this sample the
   sign of the relationship is inverted: strength is priced, and joining it pays
   less than fading it (see :class:`~src.entry.open_fade.OpenFade`).

   Six sessions of 45 heavily-correlated instruments is nonetheless a thin,
   non-independent sample. Live evaluation over more sessions is exactly the
   right way to settle it — which is why this module exists.

The setup
---------

1. **A strong trend (hard gate).** The implied move of the least-squares fit
   over ``trend_period`` must reach ``min_trend_pct`` in the traded direction —
   this is what sets the direction: an up-trend is bought, a down-trend sold.
2. **That trend must be clean (hard gate).** R² over the same window must reach
   ``min_r_squared``.
3. **Still going at mid-range (hard gate).** The ``mid_period`` fit must also
   reach ``min_mid_trend_pct`` in the same direction, so the move is current and
   not an hour-old memory.
4. **A pause, not an extension (hard gate).** Two conditions, and they are the
   distinctive part of the signature: the short ``entry_period`` fit must be
   *flatter* than ``max_entry_trend_pct``, and the ``roc_period`` rate of change
   must be flat or **against** the trade. This is the "let it breathe" step —
   the 16th-percentile ``roc5`` in the table above.
5. **Below the extreme, but not far (hard gate).** Distance from the favourable
   channel extreme must land inside
   ``[min_extreme_distance_atr, max_extreme_distance_atr]`` — never a breakout
   at the extreme, never a collapsed trend far beneath it.
6. **The instrument must move (hard gate).** ATR/price ≥ ``min_atr_pct``.

Divergence from the observed session, stated plainly: the five manual SELLs of
2026-07-24 were *continuations* (their short-term slope was aligned with the
trade, at the 92nd percentile), not pull-backs. Mirroring the BUY gesture is the
coherent design and is what this module implements, so its SELL side is a
hypothesis about the trader's intent rather than a replay of what was traded.
The continuation variant measured 30.98 %, no better.

Selection-layer behaviour
-------------------------

- ``emits_shorts = True`` — two-sided by construction.
- ``wallet_bounded = True`` — keep opening while the wallet has funds. This
  strategy also expects ``ALLOW_SAME_DAY_REOPEN=true`` in ``.env`` (a global
  policy, no longer a strategy knob) to keep the candidate count high (~170
  distinct episodes/day measured across the universe).
- ``open_cooldown_minutes = 3`` — spacing, so a sector trending together is not
  opened as a single de-facto position.

Documented in ``docs/strategies/open_pullback.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.core.indicators import atr, channel_position, rate_of_change, trend_pct
from src.entry.base import EntryIntent, EntryStrategy
from src.feed.price_buffer import EpicBuffer

logger = logging.getLogger(__name__)


def _clamp01(value: float) -> float:
    """Clamp ``value`` to the closed unit interval [0, 1]."""
    return max(0.0, min(1.0, value))


def _tent(x: float, low: float, high: float) -> float:
    """Sweet-spot score in [0, 1]: 0 at ``low``/``high``, 1 at their midpoint."""
    if not low < x < high:
        return 0.0
    mid = (low + high) / 2
    return (x - low) / (mid - low) if x < mid else (high - x) / (high - mid)


@dataclass
class OpenPullback(EntryStrategy):
    """Rank markets by how cleanly they are pausing inside a strong trend."""

    name = "open_pullback"
    cross_epic_selection = True
    emits_shorts = True  # symmetric: buys the dip in an up-trend, sells the pop

    # Rolling-selection constants (read by the scheduler).
    wallet_bounded = True
    concurrent_positions = 1  # fallback cap only, used when the balance is unknown
    open_cooldown_minutes = 3
    open_after_minutes = 60
    wallet_reserve = 0.10
    min_participation_ratio = 0.5

    # Windows (candles ≈ minutes on the one-minute feed).
    trend_period: int = 60  # the trend being joined, and the channel it sits in
    mid_period: int = 30  # confirms the move is still under way
    entry_period: int = 15  # the short leg that must have gone flat
    roc_period: int = 5  # the pause / pull-back measurement
    atr_period: int = 14

    # Hard gates — the observed manual signature.
    min_trend_pct: float = 0.40  # 95th baseline percentile of the manual entries
    min_r_squared: float = 0.70  # the trend must be a clean line
    min_mid_trend_pct: float = 0.20  # still going at 30 candles
    max_entry_trend_pct: float = 0.30  # short leg flat -> not extended
    max_roc_pct: float = 0.0  # last candles flat or against us: the pause
    min_extreme_distance_atr: float = 0.5  # never enter at the extreme
    max_extreme_distance_atr: float = 3.5  # nor far beneath a collapsed trend
    min_atr_pct: float = 0.04  # the instrument must move

    # Score shaping (ranking only — never gates).
    trend_pct_target: float = 2.50  # trend strength (%) at which the score saturates
    max_spread_ratio: float = 0.0015

    # Composite weights, summing to 1.0 so the score stays in [0, 1].
    weight_trend: float = 0.35  # strength of the trend being joined
    weight_cleanliness: float = 0.30  # how linear it is
    weight_entry: float = 0.25  # how well-placed the pull-back entry is
    weight_spread: float = 0.10  # cheaper-to-trade tie-breaker

    @property
    def warmup(self) -> int:
        return (
            max(
                self.trend_period,
                self.mid_period,
                self.entry_period,
                self.roc_period,
                self.atr_period,
            )
            + 1
        )

    @classmethod
    def from_settings(cls, settings) -> OpenPullback:
        # All parameters are constants of this class (the dataclass field
        # defaults above); tune by editing them here.
        return cls()

    def evaluate(self, epic: str, buf: EpicBuffer) -> EntryIntent | None:
        candles = list(buf.candles)
        if len(candles) < self.warmup:
            return None
        last = candles[-1]
        bid = last.bid_close
        if bid <= 0:
            return None

        atr_value = atr(candles, self.atr_period)
        if atr_value <= 0:
            return None

        # Gate — the instrument must actually move.
        atr_pct = atr_value / bid * 100
        if atr_pct < self.min_atr_pct:
            return None

        closes = buf.bid_closes

        # The trend to join sets the direction: up-trend bought, down-trend sold.
        move, r_squared = trend_pct(closes, self.trend_period)
        if abs(move) < self.min_trend_pct:
            return None
        direction = "BUY" if move > 0 else "SELL"
        sign = 1.0 if direction == "BUY" else -1.0

        # Gate — that trend must be a clean line.
        if r_squared < self.min_r_squared:
            logger.debug(
                "Pullback %s rejected: trend not clean (R²=%.2f < %.2f)",
                epic,
                r_squared,
                self.min_r_squared,
            )
            return None

        # Gate — still going at mid-range, so we are joining a live move.
        mid_move, _ = trend_pct(closes, self.mid_period)
        if sign * mid_move < self.min_mid_trend_pct:
            logger.debug(
                "Pullback %s rejected: move stalled at %d candles (%.2f%%)",
                epic,
                self.mid_period,
                mid_move,
            )
            return None

        # Gate — the pause. The short leg must be flat (not extended) AND the
        # very last candles flat or against us. This is the distinctive half of
        # the signature: we join on the breath, not on the thrust.
        entry_move, _ = trend_pct(closes, self.entry_period)
        if sign * entry_move > self.max_entry_trend_pct:
            logger.debug(
                "Pullback %s rejected: short leg extended (%.2f%%)", epic, entry_move
            )
            return None
        roc = sign * rate_of_change(closes, self.roc_period)
        if roc > self.max_roc_pct:
            logger.debug("Pullback %s rejected: no pause (roc=%.3f%%)", epic, roc)
            return None

        # Gate — below the favourable extreme, but not far. For a BUY that is the
        # channel high, for a SELL the channel low; ``distance`` is expressed in
        # ATRs and is always positive (price is on the near side of the extreme).
        raw_pos, high, low = channel_position(candles, self.trend_period)
        extreme = high if direction == "BUY" else low
        distance = abs(extreme - bid) / atr_value
        if not (
            self.min_extreme_distance_atr <= distance <= self.max_extreme_distance_atr
        ):
            logger.debug(
                "Pullback %s rejected: %.2f ATR from the extreme (want %.1f-%.1f)",
                epic,
                distance,
                self.min_extreme_distance_atr,
                self.max_extreme_distance_atr,
            )
            return None

        # --- ranking only, past this point ---

        trend_score = _clamp01(
            abs(move) / self.trend_pct_target if self.trend_pct_target > 0 else 0.0
        )
        cleanliness = _clamp01(r_squared)
        # Best placed in the middle of the allowed pull-back band.
        entry_score = _tent(
            distance, self.min_extreme_distance_atr, self.max_extreme_distance_atr
        )
        spread_quality = (
            _clamp01(1.0 - (last.spread / bid) / self.max_spread_ratio)
            if self.max_spread_ratio > 0
            else 0.0
        )

        score = (
            self.weight_trend * trend_score
            + self.weight_cleanliness * cleanliness
            + self.weight_entry * entry_score
            + self.weight_spread * spread_quality
        )

        logger.debug(
            "Pullback %s: %s score=%.3f (move=%.2f%% R²=%.2f mid=%.2f%% "
            "entry=%.2f%% roc=%.3f%% dist=%.2fATR)",
            epic,
            direction,
            score,
            move,
            r_squared,
            mid_move,
            entry_move,
            roc,
            distance,
        )
        return EntryIntent(epic=epic, direction=direction, score=score)
