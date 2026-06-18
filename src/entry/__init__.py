"""Entry-strategy registry — pick the *open* idea by name.

The orchestration layer asks this registry for an
:class:`~src.entry.base.EntryStrategy` and composes it with an independently
chosen :class:`~src.exit.base.CloseProfile`. Selecting an entry is a one-line
config change (``ENTRY_STRATEGY_NAME`` in ``.env``).

Adding an entry strategy:

1. implement it in ``src/entry/<name>.py`` (subclass ``EntryStrategy``);
2. register the class in :data:`ENTRY_STRATEGIES` below;
3. document it in ``docs/strategies/<name>.md``;
4. add its parameters to ``src/config.py`` (and ``.env.example``).
"""

from __future__ import annotations

from src.entry.base import EntryIntent, EntryStrategy
from src.entry.donchian_er import DonchianEntry

#: Name → class map. Keys are the valid ``ENTRY_STRATEGY_NAME`` values.
ENTRY_STRATEGIES: dict[str, type[EntryStrategy]] = {
    DonchianEntry.name: DonchianEntry,
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
    "DonchianEntry",
    "EntryIntent",
    "EntryStrategy",
    "get_entry_strategy",
]
