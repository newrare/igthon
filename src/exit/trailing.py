"""Pure close-rule maths for the exit domain.

These helpers carry no I/O and no dependency on the trading service: they take
the live price plus the position's persisted levels and return a decision or a
new stop level. They are shared by the live close path
(:class:`~src.execution.trading.TradingService`), the
:class:`~src.exit.atr_trailing.AtrTrailingExit` close profile, and the
simulator — which is exactly why they live in the exit domain rather than in
the trading service.

``config`` is duck-typed (:class:`TrailingConfig`): any object exposing
``atr_k_pre`` / ``atr_k_post`` / ``trailing_step_ratio`` works, so both the
live ``TradeConfig`` and a ``CloseProfile`` can drive the same maths.
"""

from __future__ import annotations

from typing import Protocol


class TrailingConfig(Protocol):
    """Minimal config surface needed by :func:`compute_trailing_stop`."""

    atr_k_pre: float
    atr_k_post: float
    trailing_step_ratio: float


def decide_close_reason(
    current_bid: float,
    *,
    level_win: float,
    level_loose: float,
    is_close_hour: bool,
) -> str | None:
    """Pure close-rule evaluation shared by live trading and the simulator.

    Returns the close reason ("end_of_day" | "win" | "loose") or None when the
    position should stay open (the follower/trailing update is handled
    separately by the caller).

    ``level_win = 0`` means there is no fixed take-profit (e.g. the Donchian
    breakout rides its trailing stop) — the win check is skipped.
    """
    if is_close_hour:
        return "end_of_day"
    if level_win > 0 and current_bid >= level_win:
        return "win"
    if level_loose > 0 and current_bid <= level_loose:
        return "loose"
    return None


def clamp_trailing_distance(
    raw_distance: float,
    *,
    spread: float,
    euro_per_point: float,
    euro_stop: float,
) -> float:
    """Bound the trailing distance between two safety limits.

    Floor: a couple of spreads, so the bid/offer oscillation alone cannot
    trigger the stop (avoids closing on noise). Ceiling: the initial planned
    euro risk (``euro_stop`` / ``euro_per_point``), so the trailing stop is
    never further from price than the loss accepted at open.
    """
    distance = raw_distance
    if euro_per_point > 0 and euro_stop > 0:
        distance = min(distance, euro_stop / euro_per_point)
    floor = max(spread * 2.0, 0.0)
    return max(distance, floor)


def compute_trailing_stop(
    current_bid: float,
    *,
    atr_value: float,
    spread: float,
    level_zero: float,
    level_follower: float,
    euro_per_point: float,
    euro_stop: float,
    config: TrailingConfig,
) -> float | None:
    """Pure ATR chandelier trailing-stop shared by live trading and the simulator.

    The stop trails ``k × ATR`` below price and only ever ratchets up, so it sits
    ``k × ATR`` below the running high. ``k`` can differ before/after break-even
    (``atr_k_pre`` / ``atr_k_post``), but the application keeps them EQUAL: for a
    trend-following breakout, tightening after break-even cuts winners short. The
    capability is retained so the two-speed regime can still be configured.

    Returns:
        The new stop level, or None when no update is warranted.
    """
    if atr_value <= 0:
        return None

    past_zero = level_zero > 0 and current_bid >= level_zero
    k = config.atr_k_post if past_zero else config.atr_k_pre
    distance = clamp_trailing_distance(
        k * atr_value,
        spread=spread,
        euro_per_point=euro_per_point,
        euro_stop=euro_stop,
    )

    # Trail a full ATR distance below price. The ratchet below ensures the stop
    # only ever moves up, so once the trail has climbed past break-even it stays
    # there — break-even is locked organically as the trade runs. The stop is
    # deliberately NOT pinned to ``level_zero`` on the first tick of profit:
    # doing so parked it on the entry price and a single spread of pullback
    # closed the trade flat (the "everything exits at 0 €" pathology).
    new_stop = current_bid - distance

    # Ratchet: only move up, and only when the gain is worth an API write.
    step = config.trailing_step_ratio * atr_value
    if new_stop <= level_follower + step:
        return None
    return new_stop
