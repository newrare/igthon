"""Support-based initial stop, with the profit-gated trailing left untouched.

:class:`SupportAtrProfitExit` is a thin variant of
:class:`~src.exit.atr_trailing_profit.AtrTrailingProfitExit`. It changes **only
where the protective stop is first placed at open**; every per-tick decision
afterwards — the profit gate, the momentum confirmation, the ATR chandelier that
follows the bid up, the dead-band rule and the close triggers — is inherited
from the parent *unchanged*, because that trailing behaviour works well in live
trading and must not move.

Why a new initial stop?
-----------------------
The reference profile places the stop a flat ``stop_atr_k × ATR(14)`` below the
entry. On 1-minute candles that ATR spans only ~14 minutes, so after a quiet
patch the stop is glued to the entry and ordinary bid/offer noise closes the
position before the trade can breathe. The fix is to anchor the stop **below a
real support level** and to make its distance **per-epic and noise-aware**.

Weighted support (the noise measure)
------------------------------------
Rather than the single lowest bid low of the window — which one freak wick can
drag to an extreme, putting an absurd amount at risk on that trade — the support
is a **recency-weighted low quantile** of the last ``stop_lookback`` bid lows
(see :func:`weighted_support`):

* a lone spike low is outvoted by the mass of the distribution, so the stop sits
  under the level the market *actually defends*, not under a one-off wick;
* recent candles weigh more than hour-old ones, so the support tracks the level
  being defended *now*.

The stop is then that support minus a small ``stop_buffer_atr_k × ATR`` cushion,
so a wick that just tags the support does not stop us out.

Distance floor (never tighter than today)
-----------------------------------------
The final distance is floored at ``max(min_stop_atr_k × ATR, min_stop_spread_k
× spread)`` — i.e. **never tighter** than the reference profile's stop and never
inside a couple of spreads. So this profile can only ever place the stop *at or
wider* than the current behaviour, which is exactly the "too tight" complaint it
addresses. There is deliberately **no upper cap** on the distance (a far support
is respected); the ``euro_loss_max`` open gate bounds the resulting euro risk.

Only the BUY initial stop is re-derived (the live pipeline is long-only); a SELL
keeps the inherited ATR stop.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.indicators import atr
from src.exit.atr_trailing_profit import AtrTrailingProfitExit
from src.exit.base import OpenPlan
from src.feed.price_buffer import EpicBuffer


def _clamp01(value: float) -> float:
    """Clamp ``value`` to the closed unit interval [0, 1]."""
    return max(0.0, min(1.0, value))


def weighted_support(
    lows: list[float],
    *,
    percentile: float = 0.10,
    recency_half_life: float = 30.0,
) -> float:
    """Recency-weighted low quantile of a bid-low series — a robust support.

    ``lows`` is the candle bid lows over the support window, oldest first. The
    support is the weighted ``percentile`` quantile of those lows using
    exponential recency weights: the most recent candle has weight ``1.0`` and
    weights halve every ``recency_half_life`` candles into the past. Compared to
    a plain ``min(lows)`` this gives two robustness properties:

    * a single aberrant wick below the market is outvoted by the mass of the
      distribution — the support is not dragged down to a one-off spike;
    * recent structure counts more than hour-old lows, so the support follows
      the level the market is currently defending.

    Args:
        lows: Candle bid lows, oldest first (at least one value).
        percentile: Target quantile in [0, 1]; lower → nearer the bottom of the
            low distribution (wider stop). Clamped to [0, 1].
        recency_half_life: Half-life of the recency weighting, in candles. ``0``
            (or negative) disables it and every candle weighs the same.

    Returns:
        The weighted-quantile low. The single value when ``lows`` has length 1.

    Raises:
        ValueError: when ``lows`` is empty.
    """
    if not lows:
        raise ValueError("weighted_support requires at least one low")
    n = len(lows)
    if recency_half_life > 0:
        # Newest candle (index n-1) weighs 1.0; older candles decay by half every
        # ``recency_half_life`` candles.
        weights = [0.5 ** ((n - 1 - i) / recency_half_life) for i in range(n)]
    else:
        weights = [1.0] * n
    # Weighted quantile: sort by low ascending, walk the cumulative weight until
    # it reaches ``percentile`` of the total weight.
    pairs = sorted(zip(lows, weights), key=lambda pair: pair[0])
    total = sum(weight for _, weight in pairs)
    target = _clamp01(percentile) * total
    cumulative = 0.0
    for low, weight in pairs:
        cumulative += weight
        if cumulative >= target:
            return low
    return pairs[-1][0]


@dataclass
class SupportAtrProfitExit(AtrTrailingProfitExit):
    """Support-anchored initial stop; profit-gated ATR trailing inherited as-is."""

    name = "support_atr_profit"

    # --- Initial-stop knobs (this profile only). The trailing knobs
    # (atr_k_pre/atr_k_post/noise_k/...) are inherited from the parent unchanged.
    # Defaults tuned on a 6-day recorded-candle backtest (see
    # docs/strategies/support-atr-profit.md): cap=4×ATR + the 20th percentile
    # roughly matched the reference profile's return while cutting noise stop-outs
    # ~in half and lifting the win rate from 35% to 50%.
    stop_lookback: int = 60  # support window (candles ≈ last hour on 1-min data)
    stop_buffer_atr_k: float = 0.5  # ATR cushion placed below the detected support
    support_percentile: float = 0.20  # weighted low quantile → robust support
    support_recency_half_life: float = 30.0  # recency weighting, in candles
    min_stop_atr_k: float = 2.5  # distance floor (× ATR) — never tighter than this
    min_stop_spread_k: float = 2.0  # distance floor (× spread) — never inside noise
    max_stop_atr_k: float = 4.0  # distance cap (× ATR); 0 = no cap (support as-is)

    @classmethod
    def from_settings(cls, settings) -> SupportAtrProfitExit:
        # Parameters are constants of this class (the field defaults above), so
        # the profile builds from those and ignores ``settings``. Tune by editing
        # the constants; select the profile at runtime from the dashboard.
        return cls()

    def initial_plan(
        self, *, entry_level: float, direction: str, buf: EpicBuffer
    ) -> OpenPlan:
        """Anchor the initial stop below the weighted support (BUY only).

        Reuses the parent's plan for ``level_zero``, ``target_level`` and the
        open-frozen ``level_margin`` (all independent of the stop), then replaces
        ``stop_level`` with the support-based distance, floored so it is never
        tighter than the reference ATR stop.
        """
        plan = super().initial_plan(
            entry_level=entry_level, direction=direction, buf=buf
        )
        if direction == "SELL":
            # Long-only live pipeline; keep the inherited ATR stop for SELL.
            return plan

        candles = list(buf.candles)
        atr_value = atr(candles, self.atr_period)
        last = buf.last
        spread = last.spread if last else 0.0

        lows = [candle.bid_low for candle in candles[-self.stop_lookback :]]
        support = (
            weighted_support(
                lows,
                percentile=self.support_percentile,
                recency_half_life=self.support_recency_half_life,
            )
            if lows
            else entry_level
        )

        raw_stop = support - self.stop_buffer_atr_k * atr_value
        # Floor the distance: never tighter than the reference ATR stop, never
        # inside a couple of spreads. No upper cap — euro_loss_max bounds risk.
        min_distance = max(
            self.min_stop_atr_k * atr_value,
            self.min_stop_spread_k * spread,
        )
        distance = max(entry_level - raw_stop, min_distance)
        # Optional upper cap: clip a far support to ``max_stop_atr_k × ATR`` so a
        # single deep-support trade cannot risk the whole hourly range. The floor
        # always wins over the cap, so a misconfigured cap can never tighten the
        # stop below ``min_distance``.
        if self.max_stop_atr_k > 0:
            distance = min(distance, max(self.max_stop_atr_k * atr_value, min_distance))
        plan.stop_level = entry_level - distance
        return plan
