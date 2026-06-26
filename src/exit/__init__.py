"""Close-profile registry — pick the *exit* scenario by name.

The orchestration layer asks this registry for a
:class:`~src.exit.base.CloseProfile` and composes it with an independently
chosen :class:`~src.entry.base.EntryStrategy`. Selecting an exit is a one-line
config change (``CLOSE_PROFILE_NAME`` in ``.env``), and a position remembers
which profile opened it so the same exit manages it for its whole life.

Adding a close profile:

1. implement it in ``src/exit/<name>.py`` (subclass ``CloseProfile``);
2. register the class in :data:`CLOSE_PROFILES` below;
3. document it in ``docs/strategies/`` if it has tunable parameters;
4. add its parameters to ``src/config.py`` (and ``.env.example``).
"""

from __future__ import annotations

from src.exit.atr_trailing import AtrTrailingExit
from src.exit.atr_trailing_positive import AtrTrailingPositiveExit
from src.exit.atr_trailing_profit import AtrTrailingProfitExit
from src.exit.base import CloseDecision, CloseProfile, OpenPlan

#: Name → class map. Keys are the valid ``CLOSE_PROFILE_NAME`` values.
CLOSE_PROFILES: dict[str, type[CloseProfile]] = {
    AtrTrailingExit.name: AtrTrailingExit,
    AtrTrailingPositiveExit.name: AtrTrailingPositiveExit,
    AtrTrailingProfitExit.name: AtrTrailingProfitExit,
}


def get_close_profile(name: str, settings) -> CloseProfile:
    """Build the close profile registered under ``name`` from settings.

    Raises:
        ValueError: when ``name`` is not a registered close profile.
    """
    cls = CLOSE_PROFILES.get(name)
    if cls is None:
        raise ValueError(
            f"Unknown close profile: {name!r} (available: {sorted(CLOSE_PROFILES)})"
        )
    return cls.from_settings(settings)


__all__ = [
    "CLOSE_PROFILES",
    "AtrTrailingExit",
    "AtrTrailingPositiveExit",
    "AtrTrailingProfitExit",
    "CloseDecision",
    "CloseProfile",
    "OpenPlan",
    "get_close_profile",
]
