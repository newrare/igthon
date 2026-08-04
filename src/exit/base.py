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

from src.feed.price_buffer import EpicBuffer

#: A position should stay open; nothing to do this tick.
ACTION_HOLD = "HOLD"
#: The position must be closed now (see :attr:`CloseDecision.reason`).
ACTION_CLOSE = "CLOSE"
#: The protective stop should ratchet to :attr:`CloseDecision.new_stop_level`.
ACTION_UPDATE_STOP = "UPDATE_STOP"


def noise_margin(noise_k: float, atr_value: float) -> float:
    """Smallest move that counts as real profit rather than bid churn.

    ``noise_k × ATR`` — the break-even→profit boundary. Sized purely on the bid's
    own movement/noise (ATR); the bid/offer spread is deliberately **not** a
    factor, so a wide-spread instrument does not get an inflated margin band.
    Shared by the long close profile and the mirrored short profile so both size
    it the same.
    """
    return noise_k * atr_value


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
        level_margin: Absolute "margin" level frozen at open — break-even plus
            the profile's noise margin (``0.0`` when the profile has no such
            band). Persisted so the band the stop must clear never drifts with
            later volatility. Used by the profit-trailing zone updater to forbid
            parking the stop between break-even and this level.
        profile: Name of the close profile that produced this plan; persisted on
            the position so the same profile manages it for its whole life.
    """

    stop_level: float
    level_zero: float
    target_level: float = 0.0
    level_margin: float = 0.0
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

    #: Stable profile identifier persisted on the position (snake_case). The exit
    #: behaviour is selected per zone via ``CLOSE_ZONESTART`` / ``CLOSE_ZONEMARGE``
    #: / ``CLOSE_ZONEPROFIT`` (see :mod:`src.exit`), not by this name.
    name: str = "base"

    @classmethod
    @abstractmethod
    def from_settings(cls, settings) -> CloseProfile:
        """Build the profile from application :class:`~src.core.config.Settings`."""

    @abstractmethod
    def initial_plan(
        self,
        *,
        entry_level: float,
        direction: str,
        buf: EpicBuffer,
        day_extreme: float | None = None,
    ) -> OpenPlan:
        """Choose the initial stop / target for a position about to open.

        ``day_extreme`` is forwarded untouched to the composed
        :class:`~src.stops.base.StopDistance`: the session extreme it carries lives
        outside ``buf`` and only the caller can read it. Optional, so a caller with
        no session history (tests, a curve generator) simply omits it.
        """

    @abstractmethod
    def evaluate(
        self,
        position,
        current_bid: float,
        buf: EpicBuffer,
        *,
        is_close_hour: bool,
        group_tighten: float | None = None,
    ) -> CloseDecision:
        """Decide what to do with an open position on this tick.

        ``group_tighten`` is the pre-resolved stop level from a group-aware
        profile's portfolio pre-pass (see :meth:`plan_group`); profiles that make
        only per-position decisions ignore it.
        """

    @property
    def is_group_aware(self) -> bool:
        """True when this profile makes a portfolio-level (whole-book) decision.

        A group-aware profile (``smartgroup`` in zone 1) needs a pre-pass over
        every open position before the per-position :meth:`evaluate` runs. The
        base profile decides per-position only, so this is ``False`` and the
        caller (the monitor loop) skips the pre-pass entirely.
        """
        return False

    def group_member(self, position, current_bid: float, buf: EpicBuffer):
        """Extract this position's scalars for the group pre-pass, or ``None``.

        Returns a lightweight member view consumed by :meth:`plan_group`, or
        ``None`` when the position does not participate (not group-aware, wrong
        side, or no live candle). The base profile is never group-aware, so this
        returns ``None``.
        """
        return None

    def plan_group(self, members: list) -> dict[int, float]:
        """Decide the whole-book stop tightening for this tick.

        Maps ``position_id -> new absolute stop level`` for the positions the
        group pre-pass elects to tighten; positions absent from the map hold.
        The base profile makes no group decision, so this returns an empty map.
        """
        return {}

    def current_zone(self, position, current_bid: float, buf: EpicBuffer):
        """Which price zone the live bid sits in, or ``None`` if not applicable.

        Powers the dashboard's manual stop-raise "hold": the zone the bid is in
        when the user places a stop is captured, and the automatic ratcheting is
        held until the bid crosses into a different zone. Profiles that split the
        price axis into zones (see :mod:`src.exit.zones`) override this; the base
        returns ``None`` (no zone concept — the manual stop is set once and the
        normal ratchet invariant takes over).
        """
        return None
