"""Execution domain — the *hands* of the bot.

This domain turns a decision into an effect against the broker and the database,
without making any strategic decision of its own:

- :mod:`src.execution.risk` — pure portfolio/risk gates and sizing.
- :mod:`src.execution.trading` (``TradingService``) — order placement,
  trailing-stop pushes, position close, and IG↔DB reconciliation. It is wired
  with an entry strategy and a close profile and simply executes what they
  decide.

The open path composes an :class:`~src.entry.base.EntryStrategy` (direction)
with a :class:`~src.exit.base.CloseProfile` (the stop); the close path delegates
each tick to that same profile. Execution never couples the two.
"""

from __future__ import annotations

from src.execution.risk import compute_quantity_multiplier, evaluate_open_gates

__all__ = [
    "compute_quantity_multiplier",
    "evaluate_open_gates",
]
