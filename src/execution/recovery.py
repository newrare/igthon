"""Loss-recovery detection — the "trend-reversal at open" pattern.

When a long is opened right as the trend turns, the bid never crosses break-even,
drifts straight down to the initial stop and the position closes quickly at a
loss. This module holds the pure predicate that recognises that pattern on a
*closed* position, plus the constants that shape the recovery.

The orchestration (open the double-size SELL, serialise it against the ranking
selector so it takes the single open slot) lives in the scheduler; the SELL open
itself is :meth:`~src.execution.trading.TradingService.open_recovery_short`, and
the short is then managed by
:class:`~src.exit.recovery_short.RecoveryShortProfile`.

Everything is gated by the ``RECOVERY_ENABLED`` master switch in ``.env``.
"""

from __future__ import annotations

from datetime import datetime

from src.models.position import Position

# Size multiplier for the recovery SELL: double the closed long's quantity, as
# the recovery bets harder on the confirmed reversal.
RECOVERY_QTY_MULTIPLIER = 2

# The favourable excursion (euros) below which the closed long is considered to
# have *never crossed break-even*. euro_max is the running best unrealized P&L,
# refreshed by the position sync from the live bid; a slightly positive epsilon
# absorbs a one-tick blip that is still noise, not a real move into profit.
EURO_MAX_EPSILON = 0.5

# "Quick loss" window: the long must have closed within this many seconds of
# opening to count as an immediate reversal (the pattern in view). A slow bleed
# to the stop over the session is a different failure and is not recovered.
RECOVERY_MAX_SECONDS = 1200.0  # 20 minutes

# Close reasons that count as a protective stop-out (as opposed to a win, an
# end-of-day sweep, or an external/never-opened reconciliation).
STOP_CLOSE_REASONS = frozenset({"stop", "loose", "security"})


def _held_seconds(position: Position) -> float | None:
    """Seconds the position was held, from ``time_open`` to ``time_close``.

    Both are stored naive-UTC times against the same ``date``. Returns ``None``
    when either bound is missing (an undated row is never "quick").
    """
    if (
        position.date is None
        or position.time_open is None
        or position.time_close is None
    ):
        return None
    opened = datetime.combine(position.date, position.time_open)
    closed = datetime.combine(position.date, position.time_close)
    return (closed - opened).total_seconds()


def is_recovery_trigger(position: Position) -> bool:
    """Whether a just-closed position matches the recoverable loss pattern.

    All must hold:

    - it is a **long** (``direction == "BUY"``) — only longs are recovered;
    - it is **not itself a recovery** (``reason_open != "recovery"``) — anti-loop:
      a recovery short that loses never spawns another recovery;
    - it **lost** (``win == 0``);
    - it closed on a **protective stop** (``reason_close`` in
      :data:`STOP_CLOSE_REASONS`), not a win / end-of-day / external close;
    - it **never crossed break-even** (``euro_max <= EURO_MAX_EPSILON``);
    - it was a **quick** loss (held less than :data:`RECOVERY_MAX_SECONDS`).
    """
    if position.direction != "BUY":
        return False
    if position.reason_open == "recovery":
        return False
    if position.win:
        return False
    if position.reason_close not in STOP_CLOSE_REASONS:
        return False
    if float(position.euro_max or 0) > EURO_MAX_EPSILON:
        return False
    held = _held_seconds(position)
    if held is None or held > RECOVERY_MAX_SECONDS:
        return False
    return True
