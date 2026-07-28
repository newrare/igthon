"""Pure pre-open gates for the execution domain.

These helpers are exit-agnostic and entry-agnostic: they answer "is the bot
allowed to open right now?" (:func:`evaluate_open_gates`). They carry no I/O and
are shared by the live trading service and the simulator, which is why they live
in the execution domain rather than the trading service.

The gates here are correctness checks only (market hours, signal direction,
duplicate-epic suppression, same-day re-open policy) — the daily P&L / win-rate
/ max-position circuit-breakers were removed.
"""

from __future__ import annotations


def evaluate_open_gates(
    *,
    epic: str,
    direction: str,
    in_trading_hours: bool,
    epic_already_open: bool,
    closes_soon: bool = False,
    allow_short: bool = False,
    epic_traded_today: bool = False,
) -> tuple[bool, str]:
    """Pure pre-open rule evaluation shared by live trading and the simulator.

    The caller gathers the live state and this function applies the rules to it.

    ``closes_soon`` is True when the epic's own market closes within the pre-open
    buffer: opening then would only pay the spread before the per-epic close rule
    force-closes the trade. Defaults to False so callers with no per-epic close
    time (e.g. the simulator) are unaffected.

    ``allow_short`` lifts the long-only restriction: automatic entry strategies
    are long-only (default ``False``), while a manual dashboard open passes
    ``True`` to permit a SELL. Flip the default here to let SELL through
    everywhere once shorts are validated on the automatic path.

    ``epic_traded_today`` is True when the epic already had an opening today (in
    either direction, still open or already closed) **and** the global
    ``ALLOW_SAME_DAY_REOPEN`` policy forbids re-using it. Callers that allow
    same-day re-opens — or that have no notion of a day (some tests) — leave it
    False. It is deliberately direction-agnostic: one opening per epic per day
    covers BUY and SELL alike.

    Returns:
        (allowed, reason) — reason explains the first failed gate.
    """
    if not in_trading_hours:
        return False, "Outside trading hours"

    if direction not in ("BUY", "SELL"):
        return False, f"Unknown direction {direction}"

    if direction == "SELL" and not allow_short:
        return False, f"Signal direction is {direction}"

    if epic_already_open:
        return False, f"Epic {epic} already open"

    if epic_traded_today:
        return False, f"Epic {epic} already traded today"

    if closes_soon:
        return False, f"Market {epic} closes soon"

    return True, "OK"
