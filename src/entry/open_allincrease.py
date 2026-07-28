"""Cross-epic ranker — open every rising market, paced and volatility-aware.

Like :class:`~src.entry.open_ranking.OpenRanking` and
:class:`~src.entry.open_saferanking.OpenSafeRanking` this is a **ranker**, not a
gate (``cross_epic_selection = True``): the scheduler scores every tradable epic,
ranks the BUY candidates and opens the best affordable ones. This module owns
only the *per-epic* half — "how strongly and cleanly is this curve rising across
several time horizons?" — and stays exit-agnostic (:meth:`evaluate` emits an
:class:`~src.entry.base.EntryIntent` carrying only the BUY direction and a
comparable opening score in [0, 1]; the stop/target/trailing belong to the
composed :class:`~src.exit.base.CloseProfile`).

What this ranker does differently
---------------------------------

It is built around three requirements that its siblings do not combine:

1. **Multi-timeframe trend, recent weighted more than old.** The score adds
   points for a bullish trend on three horizons — short (~10 min), medium
   (~1 h) and long (the whole buffered session, standing in for "24 h" — see the
   buffer note below) — combined as a weighted sum whose weights *decrease* with
   horizon length (``weight_short > weight_medium > weight_long``): the most
   recent trend carries the most weight.

2. **Volatility-aware — a flat rise cannot score high.** Each horizon's score is
   not merely "is the slope up?": it is the regression cleanliness (R² of a
   positive-slope fit) multiplied by a **magnitude** factor equal to the fitted
   net rise over the window measured in units of the market's own ATR
   (``net_rise / (ATR × rise_atr_target)``). A market that drifts up only
   slightly relative to its own volatility — "une hausse relativement plate" —
   earns a small magnitude factor and therefore a low score, however clean the
   line looks. This is the deliberate guard against crowning epics whose rise is
   flat.

3. **Re-openable and paced.** Re-opening is a *global* policy —
   ``ALLOW_SAME_DAY_REOPEN`` in ``.env``, not a strategy knob. This strategy is
   designed for ``true``: an epic is a candidate again as soon as it holds no
   open position, so the same market can be opened several times in one day. A
   concurrent second open on a still-open epic remains blocked by the shared
   ``epic_already_open`` gate. Pacing stays a class attribute read by the
   scheduler's rolling selector:
   - ``open_cooldown_minutes = 10`` — the selector opens at most one position per
     pass and waits at least ten minutes between two opens, so positions are
     spaced out rather than fired in a burst. Combined with
     ``wallet_bounded = True`` the account keeps opening the best rising market
     roughly every ten minutes until the spendable balance is exhausted.

The composite is a **weighted sum** (points are *added*, matching the "on ajoute
des points" spec), so the score stays in [0, 1] and is directly comparable across
epics and readable as a percentage. :meth:`evaluate` returns ``None`` on
*structural* grounds (too little data, non-positive bid, no measurable
volatility) or when the composite falls below ``min_score`` (default 0.70 — below
70 % no position is opened).

Buffer / "24 h" note
--------------------

The live price buffer keeps at most :data:`~src.feed.price_buffer.DEFAULT_MAX_CANDLES`
one-minute candles per epic and is reset daily, so a literal 24-hour window is
not available. The "long / 24 h" horizon is therefore realized as the **whole
buffered session** (up to ~3 h of one-minute candles); early in the day it uses
whatever history has accumulated. ``warmup`` requires only the medium window, so
the ranker can start trading about an hour into the session and the long horizon
fills in as the day progresses.

Documented in ``docs/strategies/open_allincrease.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.core.indicators import atr, linear_regression
from src.entry.base import EntryIntent, EntryStrategy
from src.feed.price_buffer import EpicBuffer

logger = logging.getLogger(__name__)


def _clamp01(value: float) -> float:
    """Clamp ``value`` to the closed unit interval [0, 1]."""
    return max(0.0, min(1.0, value))


@dataclass
class OpenAllIncrease(EntryStrategy):
    """Recency-weighted, volatility-aware multi-timeframe uptrend ranker."""

    name = "open_allincrease"
    cross_epic_selection = True

    # Rolling-selection constants (read by the scheduler). Plain class attributes
    # — not dataclass fields, not settings — so they stay constants of the
    # strategy. The strategy is selected at runtime from the dashboard / .env.
    #
    # Wallet-bounded and paced: keep opening the best-ranked affordable rising
    # market one at a time, at least ``open_cooldown_minutes`` apart, until the
    # spendable balance (available funds minus ``wallet_reserve``) can no longer
    # cover another margin. Whether the same epic may recur within the day is the
    # global ``ALLOW_SAME_DAY_REOPEN`` policy (.env) — this strategy assumes true.
    wallet_bounded = True  # open epics as long as the wallet has funds
    concurrent_positions = 1  # fallback cap only, used when the balance is unknown
    open_cooldown_minutes = 10  # wait ≥10 min between two opens; one open per pass
    open_after_minutes = 60  # ≈ one hour of livestream warm-up before first open
    wallet_reserve = 0.10  # keep 10% of available funds free
    min_participation_ratio = 0.5  # > half the warmed-up universe before crowning

    # Trend horizons (candles ≈ minutes on the one-minute feed). ``long_period``
    # is the "24 h" stand-in bounded by the buffer (see the module docstring).
    short_period: int = 10  # ~10 minutes
    medium_period: int = 60  # ~1 hour
    long_period: int = 180  # whole buffered session (~3 h cap), the "24 h" horizon
    atr_period: int = 14  # volatility window (also gates stop sizing at open)

    # Magnitude scale: the fitted net rise over a horizon, expressed in ATRs, at
    # which that horizon's magnitude factor saturates to 1. A rise small relative
    # to the market's own volatility ("relatively flat") earns a low factor.
    rise_atr_target: float = 3.0

    # Composite weights — recency-weighted (recent > old) and summing to 1.0 so
    # the score stays in [0, 1] / readable as a percentage.
    weight_short: float = 0.45
    weight_medium: float = 0.35
    weight_long: float = 0.20

    # Composite floor: below this the epic stays flat (0.70 = "score < 70 %").
    min_score: float = 0.70

    @property
    def warmup(self) -> int:
        # Only the medium window is required so the ranker can start ~1 h into the
        # session; the long horizon simply uses whatever history is available and
        # fills in toward ``long_period`` as the day progresses.
        return max(self.medium_period, self.atr_period) + 1

    @classmethod
    def from_settings(cls, settings) -> OpenAllIncrease:
        # All parameters are constants of this class (the dataclass field defaults
        # above), so the strategy builds from those and ignores ``settings``. Tune
        # by editing the constants here; select it at runtime via ``OPEN_STRATEGY``.
        return cls()

    def _trend_component(
        self, bids: list[float], atr_value: float, period: int
    ) -> float:
        """Score one horizon in [0, 1]: cleanliness × volatility-relative rise.

        Uses the last ``period`` bids (or all of them when fewer are buffered).
        Returns 0 when the horizon is not bullish (non-positive regression
        slope) or when there is no measurable volatility. Otherwise the score is
        the R² of the positive-slope fit (how *cleanly* it rises) multiplied by a
        magnitude factor — the fitted net rise over the window measured in ATRs,
        clamped against ``rise_atr_target`` — so a rise that is small relative to
        the market's own volatility ("relatively flat") scores low however tidy
        the line is.
        """
        window = bids[-period:] if len(bids) > period else bids
        if len(window) < 2 or atr_value <= 0:
            return 0.0
        reg = linear_regression(window)
        if reg.slope <= 0:
            return 0.0  # not rising over this horizon -> no points
        clean = _clamp01(reg.r_squared)
        net_rise = reg.slope * (len(window) - 1)
        strength = _clamp01(net_rise / (atr_value * self.rise_atr_target))
        return clean * strength

    def evaluate(self, epic: str, buf: EpicBuffer) -> EntryIntent | None:
        candles = list(buf.candles)
        if len(candles) < self.warmup:
            return None  # not enough history to score the curve
        last = candles[-1]
        bid = last.bid_close
        if bid <= 0:
            return None

        # A positive ATR is required structurally: without volatility the composed
        # close profile cannot size a protective stop at open (and it is the
        # denominator of every horizon's magnitude factor).
        atr_value = atr(candles, self.atr_period)
        if atr_value <= 0:
            return None

        bids = buf.bid_closes

        short = self._trend_component(bids, atr_value, self.short_period)
        medium = self._trend_component(bids, atr_value, self.medium_period)
        long = self._trend_component(bids, atr_value, self.long_period)

        score = (
            self.weight_short * short
            + self.weight_medium * medium
            + self.weight_long * long
        )

        if score < self.min_score:
            logger.debug(
                "AllIncrease %s below floor: score=%.3f < %.2f "
                "(short=%.2f medium=%.2f long=%.2f)",
                epic,
                score,
                self.min_score,
                short,
                medium,
                long,
            )
            return None

        logger.debug(
            "AllIncrease %s: score=%.3f (short=%.2f medium=%.2f long=%.2f)",
            epic,
            score,
            short,
            medium,
            long,
        )
        return EntryIntent(epic=epic, direction="BUY", score=score)
