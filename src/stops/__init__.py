"""Stop-distance registry — pick the *initial-stop placement* by name.

The orchestration layer (a close profile at open) asks this registry for a
:class:`~src.stops.base.StopDistance` and uses it to place the initial protective
stop, independently of the chosen entry idea and exit management. Selecting a
distance is a one-line config change (``STOP_STRATEGY`` in ``.env``).

Adding a stop-distance policy:

1. implement it in ``src/stops/<name>.py`` (subclass ``StopDistance``);
2. register the class in :data:`STOP_DISTANCES` below;
3. add its parameters (if any) to ``src/core/config.py`` and ``.env.example``.
"""

from __future__ import annotations

from src.stops.base import StopDistance
from src.stops.stop_atr import StopAtr
from src.stops.stop_hourlow import StopHourLow, noise_floor_distance, window_extreme
from src.stops.stop_linearspeed import StopLinearSpeed, weighted_resistance
from src.stops.stop_regression import StopRegression, residual_sigma
from src.stops.stop_shape import StopShape, classify_shape
from src.stops.stop_support import StopSupport, weighted_support

#: Name → class map. Keys are the valid ``STOP_STRATEGY`` values.
STOP_DISTANCES: dict[str, type[StopDistance]] = {
    StopAtr.name: StopAtr,
    StopSupport.name: StopSupport,
    StopRegression.name: StopRegression,
    StopLinearSpeed.name: StopLinearSpeed,
    StopHourLow.name: StopHourLow,
    StopShape.name: StopShape,
}


def get_stop_distance(name: str, settings) -> StopDistance:
    """Build the stop-distance policy registered under ``name`` from settings.

    Raises:
        ValueError: when ``name`` is not a registered stop-distance policy.
    """
    cls = STOP_DISTANCES.get(name)
    if cls is None:
        raise ValueError(
            f"Unknown stop distance: {name!r} (available: {sorted(STOP_DISTANCES)})"
        )
    return cls.from_settings(settings)


__all__ = [
    "STOP_DISTANCES",
    "StopAtr",
    "StopSupport",
    "StopRegression",
    "StopLinearSpeed",
    "StopHourLow",
    "StopShape",
    "StopDistance",
    "get_stop_distance",
    "weighted_support",
    "weighted_resistance",
    "residual_sigma",
    "noise_floor_distance",
    "window_extreme",
    "classify_shape",
]
