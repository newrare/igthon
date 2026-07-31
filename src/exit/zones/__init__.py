"""Per-zone stop updaters composed by a close profile.

The three zones (underwater / break-even band / real profit) and their updaters
are described in :mod:`src.exit.zones.base`. Each zone has its **own registry** of
selectable updaters, so the behaviour of every zone is chosen independently in
``.env``:

- ``CLOSE_ZONESTART``  → :data:`ZONESTART_UPDATERS`  (open → break-even);
- ``CLOSE_ZONEMARGE``  → :data:`ZONEMARGE_UPDATERS`  (break-even → margin);
- ``CLOSE_ZONEPROFIT`` → :data:`ZONEPROFIT_UPDATERS` (above the margin level).

:class:`~src.exit.close_zoneprofit.CloseZoneProfit` composes one updater from each
registry via :func:`build_zone_updater`. Adding a new per-zone behaviour is a new
:class:`~src.exit.zones.base.StopUpdater` subclass registered in the relevant
registry below — the other zones are untouched.
"""

from __future__ import annotations

from src.exit.zones.base import (
    BreakevenLockParams,
    StopContext,
    StopUpdater,
    StopZone,
    breakeven_lock_level,
    build_zone_updater,
    classify_zone,
)
from src.exit.zones.breakeven_band import (
    BreakevenBandStop,
    BreakevenHalfStop,
    BreakevenLockStop,
    BreakevenSafeStop,
)
from src.exit.zones.smartgroup import (
    GroupMember,
    SmartGroupParams,
    SmartGroupStop,
    candidate_stop,
    plan_group_tightening,
)
from src.exit.zones.timedlift import UnderwaterTimedLiftStop
from src.exit.zones.trailing_ratchet import TrailingRatchetStop
from src.exit.zones.underwater import UnderwaterStop, UnderwaterTrendCutStop

#: Zone 1 (open → break-even). Name → class; keys are valid ``CLOSE_ZONESTART``.
ZONESTART_UPDATERS: dict[str, type[StopUpdater]] = {
    UnderwaterStop.name: UnderwaterStop,
    UnderwaterTrendCutStop.name: UnderwaterTrendCutStop,
    UnderwaterTimedLiftStop.name: UnderwaterTimedLiftStop,
    SmartGroupStop.name: SmartGroupStop,
}

#: Zone 2 (break-even → margin). Name → class; keys are valid ``CLOSE_ZONEMARGE``.
ZONEMARGE_UPDATERS: dict[str, type[StopUpdater]] = {
    BreakevenBandStop.name: BreakevenBandStop,
    BreakevenLockStop.name: BreakevenLockStop,
    BreakevenSafeStop.name: BreakevenSafeStop,
    BreakevenHalfStop.name: BreakevenHalfStop,
}

#: Zone 3 (above margin). Name → class; keys are valid ``CLOSE_ZONEPROFIT``.
ZONEPROFIT_UPDATERS: dict[str, type[StopUpdater]] = {
    TrailingRatchetStop.name: TrailingRatchetStop,
}

__all__ = [
    "StopContext",
    "StopUpdater",
    "StopZone",
    "BreakevenLockParams",
    "breakeven_lock_level",
    "classify_zone",
    "build_zone_updater",
    "GroupMember",
    "SmartGroupParams",
    "SmartGroupStop",
    "candidate_stop",
    "plan_group_tightening",
    "UnderwaterStop",
    "UnderwaterTrendCutStop",
    "UnderwaterTimedLiftStop",
    "BreakevenBandStop",
    "BreakevenLockStop",
    "BreakevenSafeStop",
    "BreakevenHalfStop",
    "TrailingRatchetStop",
    "ZONESTART_UPDATERS",
    "ZONEMARGE_UPDATERS",
    "ZONEPROFIT_UPDATERS",
]
