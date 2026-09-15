"""Entry signals — an open the bot *proposes* instead of taking.

This is the "notify" half of the open decision. In ``OPEN_MODE=auto`` an
:class:`~src.entry.base.EntryIntent` that passes the pre-open gates is turned
into a real order. In ``OPEN_MODE=notify`` the very same intent, having passed
the very same gates, is recorded here instead: the bot raises a **signal**, the
dashboard turns it into a browser notification, and the user goes and looks at
the market before deciding to open it themselves (the dashboard Buy/Sell
buttons, which are manual opens and therefore never pass through this module).

The book is deliberately in-memory and short-lived:

* a signal is a *now* thing — "this epic is worth a look this minute". It has no
  value after its TTL, so nothing is persisted and no migration is needed;
* the dashboard polls every second, so an in-process list is always fresh;
* a restart clears the book, which is the correct behaviour: the strategy
  re-signals on its next tick if the setup is still there.

Per-epic cooldown keeps one setup from flooding the browser: while a signal for
an epic is still pending (or was dismissed less than ``cooldown`` ago) the same
epic raises nothing new.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import count

logger = logging.getLogger(__name__)

# How long a signal stays actionable. Past this it is dropped from the book: the
# market that justified it has moved on, and acting on a stale proposal is worse
# than acting on none.
DEFAULT_TTL = timedelta(minutes=15)

# Minimum delay between two signals on the same epic, counted from the moment the
# previous one was raised. Long enough that a strategy re-firing every analysis
# tick (30 s) notifies once, short enough that a genuinely new setup later in the
# session still reaches the user.
DEFAULT_COOLDOWN = timedelta(minutes=10)

# Hard cap on the book size — the oldest signals are evicted first. Only a guard
# against an unbounded list; the TTL does the real trimming.
MAX_SIGNALS = 50


@dataclass(slots=True)
class EntrySignal:
    """One proposed open, waiting for the user's decision.

    Attributes:
        id: Monotonic identifier, unique for the process lifetime. Used by the
            browser as the notification tag (so one signal notifies once) and by
            the dismiss endpoint.
        epic: Market the strategy wants opened.
        direction: ``"BUY"`` or ``"SELL"`` — what the strategy would have done.
        strategy: Name of the entry strategy that produced the intent.
        score: Diagnostic confidence figure carried by the intent (UI only).
        price: Close-out price at the moment the signal was raised, for context
            in the notification body. ``0.0`` when the buffer could not give one.
        name: Human-readable instrument name when known, else the epic.
        created_at: When the signal was raised (UTC).
        dismissed_at: When the user dismissed it (UTC), else ``None``. A
            dismissed signal leaves :meth:`SignalBook.pending` but still holds
            its epic in cooldown.
    """

    id: int
    epic: str
    direction: str
    strategy: str
    score: float = 0.0
    price: float = 0.0
    name: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    dismissed_at: datetime | None = None

    def as_dict(self) -> dict:
        """Return the JSON shape consumed by the dashboard."""
        return {
            "id": self.id,
            "epic": self.epic,
            "direction": self.direction,
            "strategy": self.strategy,
            "score": round(float(self.score), 2),
            "price": float(self.price),
            "name": self.name or self.epic,
            "created_at": self.created_at.isoformat(),
            "age_seconds": int((datetime.now(UTC) - self.created_at).total_seconds()),
        }


class SignalBook:
    """In-memory store of the opens the bot proposed and did not take."""

    def __init__(
        self,
        *,
        ttl: timedelta = DEFAULT_TTL,
        cooldown: timedelta = DEFAULT_COOLDOWN,
        max_signals: int = MAX_SIGNALS,
    ) -> None:
        self._ttl = ttl
        self._cooldown = cooldown
        self._max = max_signals
        self._signals: list[EntrySignal] = []
        self._ids = count(1)
        # Bumped on every write. The SSE stream (``/api/signals/stream``) watches
        # it instead of diffing payloads, so a browser tab sitting in the
        # background is pushed the new list the moment it changes — a hidden tab
        # has its timers throttled to about one tick a minute by the browser, so
        # the ordinary poll cannot be what delivers an alert.
        self._version = 0

    @property
    def version(self) -> int:
        """Monotonic write counter — changes whenever the book changes."""
        return self._version

    # ── writes ──────────────────────────────────────────────────────────────
    def record(
        self,
        epic: str,
        direction: str,
        *,
        strategy: str = "",
        score: float = 0.0,
        price: float = 0.0,
        name: str = "",
    ) -> EntrySignal | None:
        """Raise a signal on ``epic`` unless it is still in cooldown.

        Returns the new :class:`EntrySignal`, or ``None`` when the same epic
        already signalled recently (nothing is recorded and the caller should
        stay silent — the user has already been told about this market).
        """
        self._purge()
        if self._in_cooldown(epic):
            return None
        signal = EntrySignal(
            id=next(self._ids),
            epic=epic,
            direction=direction.upper(),
            strategy=strategy,
            score=score,
            price=price,
            name=name,
        )
        self._signals.append(signal)
        self._version += 1
        # Oldest-first eviction: the cap is a safety net, so the freshest
        # proposals are the ones worth keeping.
        if len(self._signals) > self._max:
            self._signals = self._signals[-self._max :]
        logger.info(
            "Entry signal raised [%s]: %s %s (score=%.2f) — waiting for your decision",
            strategy or "?",
            signal.direction,
            epic,
            score,
        )
        return signal

    def dismiss(self, signal_id: int) -> bool:
        """Mark one signal as handled. Returns False when it is already gone."""
        for signal in self._signals:
            if signal.id == signal_id and signal.dismissed_at is None:
                signal.dismissed_at = datetime.now(UTC)
                self._version += 1
                return True
        return False

    def dismiss_epic(self, epic: str) -> int:
        """Dismiss every pending signal on ``epic`` (e.g. the user opened it)."""
        now = datetime.now(UTC)
        dismissed = 0
        for signal in self._signals:
            if signal.epic == epic and signal.dismissed_at is None:
                signal.dismissed_at = now
                dismissed += 1
        if dismissed:
            self._version += 1
        return dismissed

    def clear(self) -> None:
        """Drop every signal (used by the "end the day" control and tests)."""
        if self._signals:
            self._version += 1
        self._signals.clear()

    # ── reads ───────────────────────────────────────────────────────────────
    def pending(self) -> list[EntrySignal]:
        """Return the live, undismissed signals — newest first."""
        self._purge()
        return [s for s in reversed(self._signals) if s.dismissed_at is None]

    def as_dicts(self) -> list[dict]:
        """Return :meth:`pending` in the dashboard's JSON shape."""
        return [s.as_dict() for s in self.pending()]

    def __len__(self) -> int:
        return len(self.pending())

    # ── internals ───────────────────────────────────────────────────────────
    def _purge(self) -> None:
        """Drop signals older than the TTL, dismissed or not."""
        horizon = datetime.now(UTC) - self._ttl
        self._signals = [s for s in self._signals if s.created_at > horizon]

    def _in_cooldown(self, epic: str) -> bool:
        """Whether ``epic`` signalled recently enough to stay silent.

        Counted from ``created_at`` (not from the dismissal) so a user who
        dismisses a signal immediately is not notified again seconds later by the
        next analysis tick.
        """
        horizon = datetime.now(UTC) - self._cooldown
        return any(s.epic == epic and s.created_at > horizon for s in self._signals)
