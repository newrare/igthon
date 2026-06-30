"""Pure portfolio/risk rules for the execution domain.

These helpers are exit-agnostic and entry-agnostic: they answer "is the bot
allowed to open right now?" (:func:`evaluate_open_gates`) and "how big should
the next trade be?" (:func:`compute_quantity_multiplier`). They carry no I/O
and are shared by the live trading service and the simulator, which is why they
live in the execution domain rather than the trading service.

``config`` is duck-typed (:class:`OpenGateConfig`): any object exposing the
listed thresholds works, so both the live ``TradeConfig`` and a test stand-in
drive the same rules.
"""

from __future__ import annotations

from typing import Protocol

from src.models.position import Position


class OpenGateConfig(Protocol):
    """Threshold surface needed by :func:`evaluate_open_gates`."""

    max_positions: int
    day_euro_finish_loose: float
    day_euro_finish_win: float
    max_trades_day: int
    min_win_rate: float
    daily_risk_enabled: bool


def evaluate_open_gates(
    *,
    epic: str,
    direction: str,
    in_trading_hours: bool,
    epic_already_open: bool,
    open_count: int,
    daily_pnl: float,
    trade_count: int,
    win_rate: float,
    config: OpenGateConfig,
) -> tuple[bool, str]:
    """Pure pre-open rule evaluation shared by live trading and the simulator.

    The caller gathers the live state (DB counts, daily P&L) and this function
    applies the rules to it.

    Returns:
        (allowed, reason) — reason explains the first failed gate.
    """
    if not in_trading_hours:
        return False, "Outside trading hours"

    if direction != "BUY":
        return False, f"Signal direction is {direction}"

    if epic_already_open:
        return False, f"Epic {epic} already open"

    if open_count >= config.max_positions:
        return False, f"Max positions reached ({open_count})"

    # Daily circuit-breakers — skippable at runtime (dev/test) via the dashboard.
    if getattr(config, "daily_risk_enabled", True):
        blocked = daily_risk_block(
            daily_pnl=daily_pnl,
            trade_count=trade_count,
            win_rate=win_rate,
            config=config,
        )
        if blocked is not None:
            return False, blocked

    return True, "OK"


def daily_risk_block(
    *,
    daily_pnl: float,
    trade_count: int,
    win_rate: float,
    config: OpenGateConfig,
) -> str | None:
    """Day-scope circuit-breakers that block *all* opening for the rest of the day.

    Returns the reason string when a daily gate has tripped, else ``None``. Split
    out of :func:`evaluate_open_gates` (which still calls it) so the dashboard can
    show *why* the bot has stopped opening without re-deriving the thresholds —
    keeping the live gate and the indicator in lockstep. Unlike the per-epic gates
    (duplicate epic, max positions), these depend only on the day's realized P&L
    and trade record, so they apply identically whatever epic would be opened next.
    """
    if daily_pnl <= config.day_euro_finish_loose:
        return f"Daily loss limit reached ({daily_pnl:.2f}€)"
    if daily_pnl >= config.day_euro_finish_win:
        return f"Daily target reached ({daily_pnl:.2f}€)"
    if trade_count >= config.max_trades_day:
        return f"Max daily trades reached ({trade_count})"
    if trade_count >= 10 and win_rate < config.min_win_rate:
        return f"Win rate too low ({win_rate:.0%} after {trade_count} trades)"
    return None


def compute_quantity_multiplier(
    closed_today: list[Position],
    *,
    base_multiplier: int,
    max_multiplier: int,
) -> int:
    """Martingale size multiplier from the day's trailing loss streak.

    Used by the ``trend_template`` hourly selector to "cover" prior losses: a
    winning last trade resets to ×1, and each *consecutive* loss at the tail of
    the day multiplies the size by ``base_multiplier`` (1 → 3 → 9 …). The result
    is capped at ``max_multiplier`` (the ``euro_loss_max`` open gate is the second
    backstop against an escalating martingale).

    Args:
        closed_today: Today's closed positions (any order; sorted here by close
            time, falling back to id, so the tail is the most recent trade).
        base_multiplier: Factor applied per consecutive loss.
        max_multiplier: Hard ceiling on the returned multiplier.

    Returns:
        ``min(base_multiplier ** k, max_multiplier)`` where ``k`` is the number
        of consecutive losers at the tail (``win == 0``); 1 when the last trade
        won or there are no closed trades yet.
    """
    ordered = sorted(
        closed_today, key=lambda p: (p.time_close or p.time_open, p.id or 0)
    )
    streak = 0
    for position in reversed(ordered):
        if (position.win or 0) > 0:
            break
        streak += 1
    return min(base_multiplier**streak, max_multiplier)
