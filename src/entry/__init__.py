"""Entry-strategy registry — pick the *open* idea by name.

The orchestration layer asks this registry for an
:class:`~src.entry.base.EntryStrategy` and composes it with an independently
chosen :class:`~src.exit.base.CloseProfile`. Selecting an entry is a one-line
config change (``OPEN_STRATEGY`` in ``.env``).

Adding an entry strategy:

1. implement it in ``src/entry/<name>.py`` (subclass ``EntryStrategy``);
2. register the class in :data:`ENTRY_STRATEGIES` below;
3. document it in ``docs/strategies/<name>.md``;
4. add its parameters to ``src/config.py`` (and ``.env.example``).
"""

from __future__ import annotations

from src.entry.base import EntryIntent, EntryStrategy
from src.entry.open_allincrease import OpenAllIncrease
from src.entry.open_donchian import OpenDonchian
from src.entry.open_projection import OpenProjection
from src.entry.open_ranking import OpenRanking
from src.entry.open_saferanking import OpenSafeRanking
from src.entry.open_testing import OpenTesting

#: Name → class map. Keys are the valid ``OPEN_STRATEGY`` values.
ENTRY_STRATEGIES: dict[str, type[EntryStrategy]] = {
    OpenAllIncrease.name: OpenAllIncrease,
    OpenDonchian.name: OpenDonchian,
    OpenProjection.name: OpenProjection,
    OpenRanking.name: OpenRanking,
    OpenSafeRanking.name: OpenSafeRanking,
    OpenTesting.name: OpenTesting,
}


def get_entry_strategy(name: str, settings) -> EntryStrategy:
    """Build the entry strategy registered under ``name`` from settings.

    Raises:
        ValueError: when ``name`` is not a registered entry strategy.
    """
    cls = ENTRY_STRATEGIES.get(name)
    if cls is None:
        raise ValueError(
            f"Unknown entry strategy: {name!r} "
            f"(available: {sorted(ENTRY_STRATEGIES)})"
        )
    return cls.from_settings(settings)


__all__ = [
    "ENTRY_STRATEGIES",
    "OpenAllIncrease",
    "OpenDonchian",
    "OpenProjection",
    "OpenRanking",
    "OpenSafeRanking",
    "OpenTesting",
    "EntryIntent",
    "EntryStrategy",
    "get_entry_strategy",
]
