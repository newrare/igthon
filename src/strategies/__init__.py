"""Strategy registry — pick the live/simulated strategy by name.

The shared infrastructure (scheduler jobs, API queue, trading service,
simulator, dashboard) is strategy-agnostic: it asks this registry for a
:class:`~src.strategies.base.BaseStrategy` instance and plugs it into the
open/close pipeline. Selecting a strategy is therefore a one-line config
change (``STRATEGY_NAME`` in ``.env``).

Adding a strategy:

1. implement it in ``src/strategies/<name>.py`` (subclass ``BaseStrategy``);
2. register the class in :data:`STRATEGIES` below;
3. document it in ``docs/strategies/<name>.md``;
4. add its parameters to ``src/config.py`` (and ``.env.example``).

Each entry is documented in ``docs/strategies/`` (one file per strategy).
"""

from __future__ import annotations

from src.strategies.base import BaseStrategy
from src.strategies.donchian import DonchianER
from src.strategies.trend_follower import TrendFollower

#: Name → class map. Keys are the valid ``STRATEGY_NAME`` values.
STRATEGIES: dict[str, type[BaseStrategy]] = {
    TrendFollower.name: TrendFollower,
    DonchianER.name: DonchianER,
}


def get_strategy(name: str, settings) -> BaseStrategy:
    """Build the strategy registered under ``name`` from application settings.

    Raises:
        ValueError: when ``name`` is not a registered strategy.
    """
    cls = STRATEGIES.get(name)
    if cls is None:
        raise ValueError(
            f"Unknown strategy: {name!r} (available: {sorted(STRATEGIES)})"
        )
    return cls.from_settings(settings)


__all__ = [
    "STRATEGIES",
    "BaseStrategy",
    "DonchianER",
    "TrendFollower",
    "get_strategy",
]
