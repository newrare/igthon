"""Exit domain — the *close* side of the decoupled trading pipeline.

A :class:`CloseProfile` owns **everything about exiting a position**: the
initial protective stop, any take-profit, and the per-tick trailing/close
decisions. It is composed at runtime with an
:class:`~src.entry.base.EntryStrategy` but knows nothing about it — it receives
only the entry level/direction at open time and the live position + market
state thereafter.

Two moments in a position's life:

- :meth:`CloseProfile.initial_plan` — called once at open. Returns an
  :class:`OpenPlan` (the absolute initial stop that drives both risk-based
  sizing and the IG order, plus an optional take-profit). **The close profile,
  not the entry strategy, chooses the stop.**
- :meth:`CloseProfile.evaluate` — called every monitor tick on an open
  position. Returns a :class:`CloseDecision`: hold, close (with a reason), or
  ratchet the stop to a new level.

Because the exit only reads market data + the persisted position, a profile can
be unit-tested on hand-built price paths with no entry strategy involved.

Profiles are selected by name through the registry in :mod:`src.exit`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from src.services.price_buffer import EpicBuffer

#: A position should stay open; nothing to do this tick.
ACTION_HOLD = "HOLD"
#: The position must be closed now (see :attr:`CloseDecision.reason`).
ACTION_CLOSE = "CLOSE"
#: The protective stop should ratchet to :attr:`CloseDecision.new_stop_level`.
ACTION_UPDATE_STOP = "UPDATE_STOP"


@dataclass(slots=True)
class OpenPlan:
    """What the close profile decides at the moment a position is opened.

    Attributes:
        stop_level: Absolute initial protective stop (below entry for a BUY).
            Drives risk-based sizing and the stop sent with the IG order.
        level_zero: Break-even reference used by two-speed trailing (the level
            past which the stop tightens). Typically the entry offer for a BUY.
        target_level: Absolute fixed take-profit, or ``0.0`` for none (the
            position then exits via the trailing stop / end-of-day only).
        profile: Name of the close profile that produced this plan; persisted on
            the position so the same profile manages it for its whole life.
    """

    stop_level: float
    level_zero: float
    target_level: float = 0.0
    profile: str = "base"


@dataclass(slots=True)
class CloseDecision:
    """What the close profile decides on a given monitor tick.

    Attributes:
        action: One of :data:`ACTION_HOLD`, :data:`ACTION_CLOSE`,
            :data:`ACTION_UPDATE_STOP`.
        reason: Close reason when ``action == ACTION_CLOSE`` (e.g. ``"win"``,
            ``"stop"``, ``"end_of_day"``).
        new_stop_level: New absolute stop when ``action == ACTION_UPDATE_STOP``.
    """

    action: str = ACTION_HOLD
    reason: str | None = None
    new_stop_level: float | None = None


class CloseProfile(ABC):
    """Self-contained exit manager for an open position."""

    #: Registry key and ``CLOSE_PROFILE_NAME`` value (kebab/snake, stable).
    name: str = "base"

    @classmethod
    @abstractmethod
    def from_settings(cls, settings) -> CloseProfile:
        """Build the profile from application :class:`~src.config.Settings`."""

    @abstractmethod
    def initial_plan(
        self, *, entry_level: float, direction: str, buf: EpicBuffer
    ) -> OpenPlan:
        """Choose the initial stop / target for a position about to open."""

    @abstractmethod
    def evaluate(
        self, position, current_bid: float, buf: EpicBuffer, *, is_close_hour: bool
    ) -> CloseDecision:
        """Decide what to do with an open position on this tick."""
