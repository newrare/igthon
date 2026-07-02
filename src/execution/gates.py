"""Pure pre-open gates for the execution domain.

These helpers are exit-agnostic and entry-agnostic: they answer "is the bot
allowed to open right now?" (:func:`evaluate_open_gates`). They carry no I/O and
are shared by the live trading service and the simulator, which is why they live
in the execution domain rather than the trading service.

The gates here are correctness checks only (market hours, signal direction,
duplicate-epic suppression) — the daily P&L / win-rate / max-position
circuit-breakers were removed.
"""

from __future__ import annotations


def evaluate_open_gates(
    *,
    epic: str,
    direction: str,
    in_trading_hours: bool,
    epic_already_open: bool,
) -> tuple[bool, str]:
    """Pure pre-open rule evaluation shared by live trading and the simulator.

    The caller gathers the live state and this function applies the rules to it.

    Returns:
        (allowed, reason) — reason explains the first failed gate.
    """
    if not in_trading_hours:
        return False, "Outside trading hours"

    if direction != "BUY":
        return False, f"Signal direction is {direction}"

    if epic_already_open:
        return False, f"Epic {epic} already open"

    return True, "OK"
