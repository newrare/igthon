"""ATR trailing-stop that only engages once the trade is in profit beyond noise.

:class:`AtrTrailingProfitExit` is the strict sibling of
:class:`~src.exit.atr_trailing_positive.AtrTrailingPositiveExit`: it shares the
reference initial stop and the upward ATR chandelier ratchet, but it has **no
underwater management at all**. The protective stop posted at open is held
untouched — never lowered, never nudged — until the price is genuinely positive
beyond the noise margin:

    activate trailing  ⇔  current_bid - entry > noise_margin

with the noise margin defined as the larger of a volatility fraction and the
irreducible spread churn::

    noise_margin = max(noise_k x ATR, 2 x spread)

Below that gate the stop stays at its initial level (a HOLD every tick); once the
gate is crossed the stop ratchets up ``k x ATR`` below the running high, where
``k`` is this profile's **dedicated** trailing width (its own ``atr_k_pre`` /
``atr_k_post`` constants, independent of ``atr_trailing`` /
``atr_trailing_positive``) — widen it to keep the stop further from the bid
against chop. Unlike the reference profile, this ratchet runs with the
initial-risk ceiling **disabled** (``euro_stop=0`` into ``compute_trailing_stop``):
it only ever engages once the trade is positive beyond the noise margin, so it
protects acquired gain rather than the risk taken at open. Keeping the ceiling
would clamp the gap to ``stop_atr_k`` and make the dedicated width a no-op;
dropping it lets ``atr_k_pre``/``atr_k_post`` actually set the bid↔stop gap (the
``2 × spread`` anti-noise floor still applies). The intent:
do not touch the stop on noise-sized profits — only start protecting once the
move is real.

On top of the profit gate, the upward ratchet also requires **momentum
confirmation**: the last two recorded bids must both be rising
(``bid[-3] < bid[-2] < bid[-1]``) before the stop is moved up. A single rising
spike produces only one up-step and is therefore ignored, which avoids pinning a
tighter stop on a one-tick peak that immediately falls back.

Finally, the stop is **never parked in the dead band** between break-even
(``level_zero``) and the margin level (``level_zero + noise_margin``): a stop in
that band would be triggered by noise alone for ~zero profit. The stop holds at
its initial protective level until a ratchet would place it *above* the margin
level; only then does it move up.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.indicators import atr
from src.exit.atr_trailing import AtrTrailingExit
from src.exit.base import (
    ACTION_CLOSE,
    ACTION_HOLD,
    ACTION_UPDATE_STOP,
    CloseDecision,
    OpenPlan,
)
from src.exit.trailing import compute_trailing_stop
from src.feed.price_buffer import EpicBuffer


@dataclass
class AtrTrailingProfitExit(AtrTrailingExit):
    """ATR trailing stop gated on being in profit beyond the noise margin."""

    name = "atr_trailing_profit"

    # This profile uses its OWN trailing width for both the pre/post multipliers,
    # so the bid↔stop gap can be tuned here without touching atr_trailing /
    # atr_trailing_positive. Override the inherited defaults explicitly.
    atr_k_pre: float = 2.5  # dedicated trailing width (x ATR)
    atr_k_post: float = 2.5  # kept equal to atr_k_pre (no post-break-even tighten)
    noise_k: float = 0.5  # noise margin = max(noise_k x ATR, 2 x spread)

    @classmethod
    def from_settings(cls, settings) -> AtrTrailingProfitExit:
        # Parameters are constants of this class (the field defaults above), so
        # the profile builds from those and ignores ``settings``. Tune by editing
        # the constants; select the profile at runtime from the dashboard.
        return cls()

    def _noise_margin(self, atr_value: float, spread: float) -> float:
        """Noise margin: the larger of a volatility fraction and a spread floor.

        ``max(noise_k × ATR, 2 × spread)`` — the smallest move that counts as
        real profit rather than bid/offer churn.
        """
        return max(self.noise_k * atr_value, spread * 2.0)

    def initial_plan(
        self, *, entry_level: float, direction: str, buf: EpicBuffer
    ) -> OpenPlan:
        """Inherit the ATR initial stop, then freeze the margin level at open.

        The margin level (break-even + noise margin) is computed once here and
        persisted on the plan, so the dead band the stop must clear is fixed for
        the position's whole life and never drifts as ATR later breathes.
        """
        plan = super().initial_plan(
            entry_level=entry_level, direction=direction, buf=buf
        )
        last = buf.last
        atr_value = atr(list(buf.candles), self.atr_period)
        spread = last.spread if last else 0.0
        plan.level_margin = plan.level_zero + self._noise_margin(atr_value, spread)
        return plan

    @staticmethod
    def _last_two_bids_rising(buf: EpicBuffer) -> bool:
        """True when the last two recorded bid moves are both upward.

        Requires ``bid[-3] < bid[-2] < bid[-1]`` (at least three recorded
        bids). A single rising spike yields only one up-step and so fails this
        check — that is exactly the one-tick peak we refuse to ratchet on.
        """
        closes = buf.bid_closes
        if len(closes) < 3:
            return False
        return closes[-3] < closes[-2] < closes[-1]

    def evaluate(
        self, position, current_bid: float, buf: EpicBuffer, *, is_close_hour: bool
    ) -> CloseDecision:
        """Hold the initial stop until in profit beyond noise, then ratchet up."""
        if is_close_hour:
            return CloseDecision(action=ACTION_CLOSE, reason="end_of_day")

        last = buf.last
        if last is None:
            return CloseDecision(action=ACTION_HOLD)
        atr_value = atr(list(buf.candles), self.atr_period)
        if atr_value <= 0:
            return CloseDecision(action=ACTION_HOLD)

        level_open = float(position.level_open or 0)
        level_zero = float(position.level_zero or 0)
        level_follower = float(position.level_follower or 0)
        spread = last.spread

        # Software backstop aligned with the current real stop (the follower):
        # the broker fills the pushed stop, this only guarantees a close if that
        # ever fails. The stop is never lowered, so this is also the initial stop.
        if level_follower > 0 and current_bid <= level_follower:
            return CloseDecision(action=ACTION_CLOSE, reason="stop")

        # Margin level frozen at open (break-even + noise margin). Fall back to a
        # per-tick computation for positions opened before it was persisted.
        level_margin = float(getattr(position, "level_margin", 0) or 0)
        if level_margin <= 0:
            level_margin = level_zero + self._noise_margin(atr_value, spread)

        # Gate: keep the initial stop untouched until the price is positive
        # beyond the noise margin — no lowering, no trend logic while not in.
        if current_bid - level_open <= self._noise_margin(atr_value, spread):
            return CloseDecision(action=ACTION_HOLD)

        # Momentum confirmation: only ratchet when the last two recorded bids are
        # both rising. A lone upward spike (one up-step) is ignored so the stop is
        # not tightened on a one-tick peak that falls back right after.
        if not self._last_two_bids_rising(buf):
            return CloseDecision(action=ACTION_HOLD)

        # In profit beyond noise → standard ATR chandelier ratchet (up only).
        # ``euro_stop=0`` deliberately disables the initial-risk ceiling in
        # ``clamp_trailing_distance``: here the ratchet only ever engages once the
        # trade is positive beyond the noise margin, so it protects *acquired
        # gain*, not the risk accepted at open. Capping the gap at the initial
        # risk distance would pin it to ``stop_atr_k`` and make this profile's
        # dedicated trailing width a no-op. Dropping the ceiling lets
        # ``atr_k_pre``/``atr_k_post`` actually set the bid↔stop gap; the
        # ``2 × spread`` anti-noise floor still applies.
        new_stop = compute_trailing_stop(
            current_bid,
            atr_value=atr_value,
            spread=spread,
            level_zero=level_zero,
            level_follower=level_follower,
            euro_per_point=float(position.euro_per_point or 0),
            euro_stop=0.0,
            config=self,
        )
        if new_stop is None:
            return CloseDecision(action=ACTION_HOLD)

        # Never park the stop in the dead band between break-even and the margin
        # level: a stop there would be triggered by noise alone for ~zero profit.
        # Only ratchet up once the new stop clears the (open-frozen) margin level;
        # until then the stop stays at its initial protective level below it.
        if new_stop <= level_margin:
            return CloseDecision(action=ACTION_HOLD)

        return CloseDecision(action=ACTION_UPDATE_STOP, new_stop_level=new_stop)
