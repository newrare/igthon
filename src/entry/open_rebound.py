"""Cross-epic ranker — buy the *rebound* off a sharp dip inside an up-trend.

Like :class:`~src.entry.open_ranking.OpenRanking`,
:class:`~src.entry.open_saferanking.OpenSafeRanking` and
:class:`~src.entry.open_allincrease.OpenAllIncrease` this is a **ranker**, not a
gate (``cross_epic_selection = True``): the scheduler scores every tradable epic,
ranks the BUY candidates and opens the best affordable ones. This module owns
only the *per-epic* half — "how closely does this curve match a rebound off a
sharp dip?" — and stays exit-agnostic (:meth:`evaluate` emits an
:class:`~src.entry.base.EntryIntent` carrying only the BUY direction and a
comparable opening score in [0, 1]; the stop/target/trailing belong to the
composed :class:`~src.exit.base.CloseProfile`).

The setup this ranker looks for
-------------------------------

A specific, recognisable shape (the spec, translated): *the day's general trend
is bullish, but there has been a sharp drop, and the market is now climbing back
up out of that drop.* In other words a "V" — a buy-the-dip entry — rather than a
fresh breakout to new highs. Concretely, scanning the last ``dip_period`` candles
the ranker locates the trough (the lowest bid) and the peak that preceded it, and
rewards the combination of:

1. **A bullish day (hard gate + component).** The least-squares slope of the bid
   over the whole buffered session must be strictly positive — a market
   *baissière sur la journée* is dropped outright — and the component score is the
   cleanliness (R²) of that up-trend, so a clean day-long climb ranks above a
   noisy one.
2. **A genuine, sharp drop (hard gate + component).** The peak→trough fall,
   measured in units of the market's own ATR, must reach ``min_drop_atr`` (else
   this is a steady climb, not a rebound setup — that is ``open_allincrease``'s
   job, so the epic is dropped). Its component score scales the drop depth against
   ``drop_atr_target`` — a *forte chute* scores higher than a shallow wobble.
3. **A rebound already under way (hard gate + component).** The bid over the last
   ``recent_period`` candles must be rising (positive slope — *le marché
   remonte*), and the component rewards the **fraction of the drop already
   recovered** on a sweet-spot curve peaking at ``rebound_ideal_frac``: a bounce
   that has only just turned scores low (unconfirmed), a healthy early-to-mid
   recovery scores highest, and a move that has fully retraced back to the old
   peak scores low again (the dip entry has been missed — that is a breakout, not
   a rebound). The component is scaled by how cleanly the recent leg rises (its
   R²), so a tidy bounce beats a jagged one.
4. **A fresh dip (component).** The trough's position within the window: a drop
   that bottomed recently (trough near the end) scores above one that bottomed
   long ago and has been grinding sideways since.
5. **Spread tightness (tie-breaker).** ``1 - (spread / bid) / max_spread_ratio``
   clamped to [0, 1] — the same soft cheaper-to-trade preference as the sibling
   rankers.

The composite is a **weighted sum** (weights sum to 1.0, so the score stays in
[0, 1] and is directly comparable across epics and readable as a percentage).
:meth:`evaluate` returns ``None`` on *structural* grounds (too little history,
non-positive bid, no measurable volatility), when the day is not rising, when no
sharp-enough drop is present, when the recent leg is not climbing back, or when
the composite falls below the optional ``min_score`` floor.

Selection-layer behaviour (spec)
--------------------------------

Three class attributes read by the scheduler's rolling selector realise the rest
of the spec directly:

- ``wallet_bounded = True`` — *on ouvre tant que le wallet le permet*: no fixed
  position count, keep opening the best-ranked affordable rebound until the
  spendable balance (available funds minus ``wallet_reserve``) can no longer cover
  another margin.
- ``open_cooldown_minutes = 5`` — *on attend 5 min avant d'ouvrir un nouvel
  epic*: the selector opens at most one position per pass and waits at least five
  minutes between two opens, so similar markets rebounding together are not opened
  in a single burst.
- ``allow_same_day_reopen = False`` (default) — *pour éviter des éventuels
  doublons de marché similaire*: an epic used once today is dropped from
  re-ranking, so the portfolio rotates across *different* markets rather than
  re-opening the same rebound. A concurrent duplicate open on a still-open epic is
  always blocked by the shared ``epic_already_open`` gate regardless of this flag
  — that is the *"on ouvre si l'epic choisi n'est actuellement ouvert"* guarantee.

Documented in ``docs/strategies/open_rebound.md``.
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


def _tent(x: float, peak: float) -> float:
    """Sweet-spot score in [0, 1]: 0 at the ends, 1 at ``x == peak``.

    A triangular ("tent") response: it rises linearly from 0 at ``x = 0`` to 1 at
    ``x = peak`` and falls linearly back to 0 at ``x = 1``. Used to reward a
    recovery fraction that sits in a healthy early-to-mid band — a bounce that has
    barely turned (``x -> 0``) or has fully retraced to the old peak (``x -> 1``)
    both score low, the ideal partial recovery scores highest.
    """
    if x <= 0.0 or x >= 1.0 or not (0.0 < peak < 1.0):
        return 0.0
    return x / peak if x < peak else (1.0 - x) / (1.0 - peak)


@dataclass
class OpenRebound(EntryStrategy):
    """Rank markets by how cleanly they are rebounding off a sharp dip in an up-day."""

    name = "open_rebound"
    cross_epic_selection = True

    # Rolling-selection constants (read by the scheduler). Plain class attributes
    # — not dataclass fields, not settings — so they stay constants of the
    # strategy. The strategy is selected at runtime via ``OPEN_STRATEGY``.
    #
    # Wallet-bounded and paced: keep opening the best-ranked affordable rebound one
    # at a time, at least ``open_cooldown_minutes`` apart, until the spendable
    # balance (available funds minus ``wallet_reserve``) can no longer cover another
    # margin. Same-day re-open stays OFF (the default) so the portfolio rotates
    # across different markets and does not double up on one rebound.
    wallet_bounded = True  # open epics as long as the wallet has funds
    concurrent_positions = 1  # fallback cap only, used when the balance is unknown
    allow_same_day_reopen = False  # one open per epic per day — rotate markets
    open_cooldown_minutes = 5  # wait ≥5 min between two opens; one open per pass
    open_after_minutes = 60  # ≈ one hour of livestream warm-up before first open
    wallet_reserve = 0.10  # keep 10% of available funds free
    min_participation_ratio = 0.5  # > half the warmed-up universe before crowning

    # Windows (candles ≈ minutes on the one-minute feed). The day-long up-trend is
    # measured over the *whole buffered session* (the "journée"), so it is not a
    # tunable window; ``dip_period`` is the lookback in which the drop + rebound is
    # located and ``recent_period`` the short leg that must currently be climbing.
    dip_period: int = 60  # ~1 h window scanned for the peak → trough → rebound
    recent_period: int = 10  # ~10 min leg that must be rising back up ("remonte")
    atr_period: int = 14  # volatility window (also gates stop sizing at open)

    # Drop shaping. The peak→trough fall is measured in ATRs: ``min_drop_atr`` is
    # the hard floor below which the move is too shallow to count as a *forte
    # chute* (the epic is dropped), and ``drop_atr_target`` is the depth at which
    # the drop component saturates to 1.
    min_drop_atr: float = 1.0  # min peak→trough fall (in ATRs) to qualify as a dip
    drop_atr_target: float = 3.0  # fall (in ATRs) earning the full drop score

    # Rebound shaping. The fraction of the drop already recovered
    # (``(current - trough) / (peak - trough)``) is scored on a tent peaking here:
    # a healthy early-to-mid recovery is ideal, an unconfirmed turn or a fully
    # retraced move both score low.
    rebound_ideal_frac: float = 0.40  # recovery fraction earning the full rebound score

    max_spread_ratio: float = 0.0015  # spread/bid at which the spread score hits 0
    min_score: float = 0.0  # composite floor; below it -> stay flat (0 = never)

    # Composite weights — a weighted sum (points added), summing to 1.0 so the
    # score stays in [0, 1] / readable as a percentage. The drop and the rebound
    # together carry the majority: this ranker is about the *shape*, and the
    # day-trend is the qualifying context rather than the driver.
    weight_trend: float = 0.30  # clean bullish day (context)
    weight_drop: float = 0.25  # a sharp drop happened
    weight_rebound: float = 0.30  # recovering out of it, on the sweet spot
    weight_recency: float = 0.05  # the dip bottomed recently
    weight_spread: float = 0.10  # cheaper-to-trade tie-breaker

    @property
    def warmup(self) -> int:
        # Bounded by the dip window so the ranker can start ~1 h into the session;
        # the day-long trend simply uses whatever history has accumulated so far.
        return max(self.dip_period, self.recent_period, self.atr_period) + 1

    @classmethod
    def from_settings(cls, settings) -> OpenRebound:
        # All parameters are constants of this class (the dataclass field defaults
        # above), so the strategy builds from those and ignores ``settings``. Tune
        # by editing the constants here; select it at runtime via ``OPEN_STRATEGY``.
        return cls()

    def evaluate(self, epic: str, buf: EpicBuffer) -> EntryIntent | None:
        candles = list(buf.candles)
        if len(candles) < self.warmup:
            return None  # not enough history to score the curve
        last = candles[-1]
        bid = last.bid_close
        spread = last.spread
        if bid <= 0:
            return None

        # A positive ATR is required structurally: without volatility the composed
        # close profile cannot size a protective stop at open, and it is the unit
        # in which the drop depth is measured.
        atr_value = atr(candles, self.atr_period)
        if atr_value <= 0:
            return None

        bids = buf.bid_closes

        # Gate 1 — bullish day. The whole-session slope must be strictly positive:
        # a market falling over the day is not a rebound candidate, it is a
        # down-trend. Refuse it outright rather than merely ranking it lower.
        day_reg = linear_regression(bids)
        if day_reg.slope <= 0:
            logger.debug(
                "Rebound %s rejected: day trend not rising (slope %.5g)",
                epic,
                day_reg.slope,
            )
            return None

        # Gate 2 — the recent leg must be climbing back up ("le marché remonte").
        recent_reg = linear_regression(bids[-self.recent_period :])
        if recent_reg.slope <= 0:
            logger.debug(
                "Rebound %s rejected: not recovering (recent slope %.5g)",
                epic,
                recent_reg.slope,
            )
            return None

        # Locate the dip within the window: the trough (lowest bid) and the peak
        # that preceded it (the high from which the drop started).
        window = bids[-self.dip_period :]
        trough_idx = min(range(len(window)), key=lambda i: window[i])
        trough = window[trough_idx]
        peak_before = max(window[: trough_idx + 1])
        drop = peak_before - trough

        # Gate 3 — a genuinely sharp drop must have occurred. A move shallower than
        # ``min_drop_atr`` ATRs is a steady climb, not a rebound setup, and belongs
        # to a different ranker.
        if drop < self.min_drop_atr * atr_value:
            logger.debug(
                "Rebound %s rejected: drop %.5g < %.2f×ATR (%.5g)",
                epic,
                drop,
                self.min_drop_atr,
                self.min_drop_atr * atr_value,
            )
            return None

        # 1. Trend — cleanliness (R²) of the rising day.
        trend = _clamp01(day_reg.r_squared)

        # 2. Drop — depth of the fall in ATRs, saturating at ``drop_atr_target``.
        drop_score = _clamp01(
            (drop / atr_value) / self.drop_atr_target
            if self.drop_atr_target > 0
            else 0.0
        )

        # 3. Rebound — fraction of the drop recovered so far, on the sweet-spot
        #    tent, scaled by how cleanly the recent leg rises (its R²).
        recovery_frac = (bid - trough) / drop if drop > 0 else 0.0
        rebound = _tent(recovery_frac, self.rebound_ideal_frac) * _clamp01(
            recent_reg.r_squared
        )

        # 4. Recency — how recently the dip bottomed (trough near the window end).
        recency = trough_idx / (len(window) - 1) if len(window) > 1 else 0.0

        # 5. Spread tightness — 1 at zero spread, 0 at/above the ceiling.
        spread_quality = (
            _clamp01(1.0 - (spread / bid) / self.max_spread_ratio)
            if self.max_spread_ratio > 0
            else 0.0
        )

        score = (
            self.weight_trend * trend
            + self.weight_drop * drop_score
            + self.weight_rebound * rebound
            + self.weight_recency * recency
            + self.weight_spread * spread_quality
        )

        if score < self.min_score:
            return None

        logger.debug(
            "Rebound %s: score=%.3f (trend=%.2f drop=%.2f[%.2fATR] rebound=%.2f"
            "[frac=%.2f] recency=%.2f spread=%.2f)",
            epic,
            score,
            trend,
            drop_score,
            drop / atr_value,
            rebound,
            recovery_frac,
            recency,
            spread_quality,
        )
        return EntryIntent(epic=epic, direction="BUY", score=score)
