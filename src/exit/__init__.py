"""Close-profile registry — the *exit* side of the decoupled pipeline.

The orchestration layer asks this module for a
:class:`~src.exit.base.CloseProfile` and composes it with an independently chosen
:class:`~src.entry.base.EntryStrategy` and — through the profile — a
:class:`~src.stops.base.StopDistance`. A position remembers which profile opened
it so the same exit manages it for its whole life.

There is a single close profile,
:class:`~src.exit.close_zoneprofit.CloseZoneProfit`. It is a **composer**: it wires
a swappable initial stop distance (:mod:`src.stops`, ``STOP_STRATEGY``) with three
**independently-selectable** per-zone stop updaters (:mod:`src.exit.zones`), one
per price zone:

- ``CLOSE_ZONESTART``  → open → break-even   (``ZONESTART_UPDATERS``);
- ``CLOSE_ZONEMARGE``  → break-even → margin (``ZONEMARGE_UPDATERS``);
- ``CLOSE_ZONEPROFIT`` → above the margin    (``ZONEPROFIT_UPDATERS``).

Each zone's behaviour is thus a one-line ``.env`` change and can be tuned without
influencing the other two. Adding a per-zone behaviour is a new
:class:`~src.exit.zones.base.StopUpdater` registered in the relevant zone registry.
"""

from __future__ import annotations

from src.exit.base import CloseDecision, CloseProfile, OpenPlan
from src.exit.close_zoneprofit import CloseZoneProfit
from src.exit.zones import (
    ZONEMARGE_UPDATERS,
    ZONEPROFIT_UPDATERS,
    ZONESTART_UPDATERS,
)


def get_close_profile(settings) -> CloseProfile:
    """Build the close profile from settings.

    There is a single composer profile; the exit behaviour is chosen through the
    three per-zone selectors (``CLOSE_ZONESTART`` / ``CLOSE_ZONEMARGE`` /
    ``CLOSE_ZONEPROFIT``) and the initial stop distance (``STOP_STRATEGY``).
    """
    return CloseZoneProfit.from_settings(settings)


__all__ = [
    "ZONESTART_UPDATERS",
    "ZONEMARGE_UPDATERS",
    "ZONEPROFIT_UPDATERS",
    "CloseZoneProfit",
    "CloseDecision",
    "CloseProfile",
    "OpenPlan",
    "get_close_profile",
]
