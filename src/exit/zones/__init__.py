"""Per-zone stop updaters composed by a close profile.

The four zones (underwater / break-even band / secure / real profit) and their
updaters are described in :mod:`src.exit.zones.base`. Each zone has its **own
registry** of selectable updaters, so the behaviour of every zone is chosen
independently in ``.env``:

- ``CLOSE_ZONESTART``  → :data:`ZONESTART_UPDATERS`  (follower → break-even);
- ``CLOSE_ZONEMARGE``  → :data:`ZONEMARGE_UPDATERS`  (break-even → margin);
- ``CLOSE_ZONESECURE`` → :data:`ZONESECURE_UPDATERS` (margin → profit trigger);
- ``CLOSE_ZONEPROFIT`` → :data:`ZONEPROFIT_UPDATERS` (above the profit trigger).

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
    BreakevenLockStop,
    BreakevenSafeStop,
    LimitLooseStop,
)
from src.exit.zones.secure import BreakevenHalfStop, SecureHoldStop
from src.exit.zones.smartgroup import (
    GroupMember,
    GroupPlanReport,
    MemberValuation,
    SmartGroupParams,
    SmartGroupStop,
    candidate_stop,
    explain_group_tightening,
    plan_group_tightening,
)
from src.exit.zones.timedlift import UnderwaterTimedLiftStop
from src.exit.zones.trailing_ratchet import (
    TrailingRatchetMoreStop,
    TrailingRatchetStop,
)
from src.exit.zones.underwater import UnderwaterStop, UnderwaterTrendCutStop

#: Zone 1 (follower → break-even). Name → class; keys are valid ``CLOSE_ZONESTART``.
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
    LimitLooseStop.name: LimitLooseStop,
}

#: Zone 3 (margin → profit trigger). Keys are valid ``CLOSE_ZONESECURE``.
ZONESECURE_UPDATERS: dict[str, type[StopUpdater]] = {
    SecureHoldStop.name: SecureHoldStop,
    BreakevenHalfStop.name: BreakevenHalfStop,
}

#: Zone 4 (above the profit trigger). Keys are valid ``CLOSE_ZONEPROFIT``.
ZONEPROFIT_UPDATERS: dict[str, type[StopUpdater]] = {
    TrailingRatchetStop.name: TrailingRatchetStop,
    TrailingRatchetMoreStop.name: TrailingRatchetMoreStop,
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
    "GroupPlanReport",
    "MemberValuation",
    "SmartGroupParams",
    "SmartGroupStop",
    "candidate_stop",
    "explain_group_tightening",
    "plan_group_tightening",
    "UnderwaterStop",
    "UnderwaterTrendCutStop",
    "UnderwaterTimedLiftStop",
    "BreakevenBandStop",
    "BreakevenLockStop",
    "BreakevenSafeStop",
    "LimitLooseStop",
    "SecureHoldStop",
    "BreakevenHalfStop",
    "TrailingRatchetStop",
    "TrailingRatchetMoreStop",
    "ZONESTART_UPDATERS",
    "ZONEMARGE_UPDATERS",
    "ZONESECURE_UPDATERS",
    "ZONEPROFIT_UPDATERS",
]
