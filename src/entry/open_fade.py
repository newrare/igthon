"""Cross-epic ranker — **fade** a clean, extended trend at the end of its channel.

Both directions. This is the mean-reversion counterpart of the trend-following
rankers (:class:`~src.entry.open_allincrease.OpenAllIncrease`,
:class:`~src.entry.open_rebound.OpenRebound`): instead of joining a move it takes
the other side of one that has run cleanly into the edge of its own range.

Where it comes from
-------------------

Derived from a measurement over the stored one-minute candles (6 sessions,
45-51 epics, ~100 000 resolved outcomes). Every point of the universe was scored
in both directions and resolved against a fixed 1.5 ATR stop / 3.0 ATR target —
so the reference to beat is the 2:1 breakeven rate of **33.33 %**, and an
unfiltered entry measured 33.63 %.

Bucketing each indicator into deciles showed the *trend-following* end of every
one of them sitting **below** breakeven and the *fading* end above it:

===============  ===================  ===================
feature          decile 1 (fade end)  decile 10 (trend end)
===============  ===================  ===================
``slope60``      35.0 %               32.4 %
``chan60``       35.4 %               31.5 %
``brk60``        35.1 %               31.9 %
===============  ===================  ===================

Stacking the fade end of those axes and splitting the sample in two halves
(train = first 5 sessions, test = last 3) gave the only rule that reproduced
out-of-sample:

============================  ==============  ==============
rule                          train           test
============================  ==============  ==============
unfiltered baseline           33.44 %         33.85 %
fade, all instrument classes  35.84 %         34.33 %
**fade, commodities only**    **37.30 %**     **37.62 %**
fade + commodities + 10-13h   40.46 %         35.71 %
============================  ==============  ==============

Two things drove the defaults below. The commodity restriction is **not
cosmetic**: measured on distinct signal episodes (one entry per contiguous
qualifying run, which is what the scheduler actually opens) the commodity-only
rule holds at 34.82 % while the all-classes version falls to 30.55 %, i.e. below
breakeven. And the hour-of-day filter was dropped despite scoring highest on
train — it decays on test, which is the signature of overfitting.

.. warning::

   Six sessions of 45 heavily-correlated instruments is a **thin, non-independent
   sample**: the effective number of observations is far below the raw count, so
   the confidence intervals implied by ``n`` are optimistic. Treat the edge above
   as a hypothesis to validate live, not as a measured expectancy.

The setup
---------

A market that has trended cleanly *away* from where we want to trade, and has
arrived at the far end of its own range:

1. **An extended trend against us (hard gate).** The implied move of the
   least-squares fit over ``trend_period`` must be at least ``min_trend_pct``
   **against** the intended direction — we buy what has been falling and sell
   what has been rising.
2. **That trend must be clean (hard gate).** Its R² must reach ``min_r_squared``.
   A stretched rubber band mean-reverts; directionless chop does not, and would
   only feed the entry noise.
3. **Price at the far end of the channel (hard gate).** ``channel_position``
   over the same window must be within ``max_channel_pos`` of the extreme we are
   fading — the bottom of the range for a BUY, the top for a SELL.
4. **The instrument must move (hard gate).** ATR as a fraction of price must
   reach ``min_atr_pct``, else the stop cannot be placed outside the noise.
5. **Commodity restriction (hard gate, on by default).** See above — set
   ``commodity_only = False`` to widen the universe, knowing the measured edge
   does not survive it.

The composite score (used only to rank candidates against each other, never to
gate) rewards a deeper stretch, a cleaner trend, a more extreme channel position
and a tighter spread.

Selection-layer behaviour
-------------------------

- ``emits_shorts = True`` — the ranker genuinely trades both ways, so the
  scheduler keeps its SELL intents and lifts the long-only pre-open gate.
- ``wallet_bounded = True`` — keep opening the best-ranked affordable candidate
  until the spendable balance is exhausted.
- ``open_cooldown_minutes = 3`` — at most one open per pass, spaced. Commodities
  move together; the 2026-07-24 session opened five correlated soft commodities
  inside 82 seconds and they peaked and collapsed as one position.
- ``ALLOW_SAME_DAY_REOPEN=true`` (global ``.env`` policy, no longer a strategy
  attribute) — an epic becomes a candidate again once it holds no open position,
  which is what keeps the candidate count high across a session (~84 distinct
  episodes/day measured on the commodity universe).

Documented in ``docs/strategies/open_fade.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.core.indicators import atr, channel_position, trend_pct
from src.entry.base import EntryIntent, EntryStrategy
from src.feed.price_buffer import EpicBuffer

logger = logging.getLogger(__name__)

#: IG epic prefixes that identify a commodity market (``CC.D.`` cash / ``CO.D.``
#: futures). The measured edge is confined to these.
COMMODITY_PREFIXES: tuple[str, ...] = ("CC.D.", "CO.D.")


def _clamp01(value: float) -> float:
    """Clamp ``value`` to the closed unit interval [0, 1]."""
    return max(0.0, min(1.0, value))


@dataclass
class OpenFade(EntryStrategy):
    """Rank markets by how cleanly they are over-extended, and take the other side."""

    name = "open_fade"
    cross_epic_selection = True
    emits_shorts = True  # genuinely two-sided: buys falls, sells rises

    # Rolling-selection constants (read by the scheduler). Plain class attributes
    # — not dataclass fields, not settings — so they stay constants of the
    # strategy, selected at runtime via ``OPEN_STRATEGY``.
    wallet_bounded = True  # open while the wallet has funds
    concurrent_positions = 1  # fallback cap only, used when the balance is unknown
    open_cooldown_minutes = 3  # spacing — correlated commodities must not fire as one
    open_after_minutes = 60
    wallet_reserve = 0.10
    min_participation_ratio = 0.5

    # Windows (candles ≈ minutes on the one-minute feed).
    trend_period: int = 60  # the trend being faded, and the channel it sits in
    atr_period: int = 14

    # Hard gates — the decile analysis above.
    min_trend_pct: float = 0.30  # implied % move of the fit, AGAINST our direction
    min_r_squared: float = 0.60  # the faded trend must be a clean line
    max_channel_pos: float = 0.30  # how close to the faded extreme we require
    min_atr_pct: float = 0.03  # ATR as % of price — the instrument must move
    commodity_only: bool = True  # the restriction the out-of-sample test demands

    # Score shaping (ranking only — never gates).
    trend_pct_target: float = 1.50  # stretch (%) at which the trend score saturates
    max_spread_ratio: float = 0.0015  # spread/bid at which the spread score hits 0

    # Composite weights, summing to 1.0 so the score stays in [0, 1].
    weight_stretch: float = 0.40  # how far the trend has run against us
    weight_cleanliness: float = 0.25  # how linear that trend is
    weight_channel: float = 0.25  # how close to the extreme we are entering
    weight_spread: float = 0.10  # cheaper-to-trade tie-breaker

    #: Overridable for tests / a different broker naming scheme.
    commodity_prefixes: tuple[str, ...] = field(default=COMMODITY_PREFIXES)

    @property
    def warmup(self) -> int:
        return max(self.trend_period, self.atr_period) + 1

    @classmethod
    def from_settings(cls, settings) -> OpenFade:
        # All parameters are constants of this class (the dataclass field
        # defaults above); tune by editing them here.
        return cls()

    def _is_commodity(self, epic: str) -> bool:
        """True when ``epic`` is a commodity market by IG's prefix convention."""
        return epic.startswith(self.commodity_prefixes)

    def evaluate(self, epic: str, buf: EpicBuffer) -> EntryIntent | None:
        candles = list(buf.candles)
        if len(candles) < self.warmup:
            return None
        last = candles[-1]
        bid = last.bid_close
        if bid <= 0:
            return None

        # Gate — universe. The out-of-sample test only holds on commodities.
        if self.commodity_only and not self._is_commodity(epic):
            return None

        # A positive ATR is required structurally: it is both the volatility gate
        # below and the unit the composed close profile sizes its stop in.
        atr_value = atr(candles, self.atr_period)
        if atr_value <= 0:
            return None

        # Gate — the instrument must actually move, else the protective stop
        # cannot be placed outside the noise.
        atr_pct = atr_value / bid * 100
        if atr_pct < self.min_atr_pct:
            return None

        # The trend to fade. ``move`` is the signed implied % move over the
        # window: negative means the market has been falling, so we BUY it.
        move, r_squared = trend_pct(buf.bid_closes, self.trend_period)
        if abs(move) < self.min_trend_pct:
            return None  # no trend worth fading
        direction = "BUY" if move < 0 else "SELL"

        # Gate — the faded trend must be a clean line, not chop.
        if r_squared < self.min_r_squared:
            logger.debug(
                "Fade %s rejected: trend not clean (R²=%.2f < %.2f)",
                epic,
                r_squared,
                self.min_r_squared,
            )
            return None

        # Gate — price must sit at the extreme we are fading: the bottom of the
        # channel for a BUY, the top for a SELL. ``favourable`` re-expresses the
        # raw 0-1 channel position from the trade's point of view, so 0 always
        # means "at the extreme we are fading".
        raw_pos, _high, _low = channel_position(candles, self.trend_period)
        favourable = raw_pos if direction == "BUY" else 1.0 - raw_pos
        if favourable > self.max_channel_pos:
            logger.debug(
                "Fade %s rejected: not at the channel extreme (pos=%.2f > %.2f)",
                epic,
                favourable,
                self.max_channel_pos,
            )
            return None

        # --- ranking only, past this point ---

        stretch = _clamp01(
            abs(move) / self.trend_pct_target if self.trend_pct_target > 0 else 0.0
        )
        cleanliness = _clamp01(r_squared)
        # 1 when hard against the faded extreme, 0 at the gate's edge.
        channel = _clamp01(
            1.0 - favourable / self.max_channel_pos if self.max_channel_pos > 0 else 0.0
        )
        spread_quality = (
            _clamp01(1.0 - (last.spread / bid) / self.max_spread_ratio)
            if self.max_spread_ratio > 0
            else 0.0
        )

        score = (
            self.weight_stretch * stretch
            + self.weight_cleanliness * cleanliness
            + self.weight_channel * channel
            + self.weight_spread * spread_quality
        )

        logger.debug(
            "Fade %s: %s score=%.3f (move=%.2f%% R²=%.2f pos=%.2f atr=%.3f%%)",
            epic,
            direction,
            score,
            move,
            r_squared,
            favourable,
            atr_pct,
        )
        return EntryIntent(epic=epic, direction=direction, score=score)
