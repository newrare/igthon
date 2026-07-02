"""Per-zone stop updaters — the *close* side split by where price sits.

A position's stop is managed differently depending on where the live bid sits
relative to two references frozen at open:

- ``level_zero`` — the break-even level (the entry offer for a BUY);
- ``level_margin`` — break-even plus the epic's noise margin, the smallest move
  that counts as real profit rather than bid/offer churn.

That splits the price axis into three zones, each its own responsibility and its
own :class:`StopUpdater` (so each can be reasoned about and unit-tested alone):

- :class:`StopZone.UNDERWATER` — ``bid <= level_zero`` —
  :class:`~src.exit.zones.underwater.UnderwaterStop`;
- :class:`StopZone.BREAKEVEN_BAND` — ``level_zero < bid <= level_margin`` —
  :class:`~src.exit.zones.breakeven_band.BreakevenBandStop`;
- :class:`StopZone.PROFIT` — ``bid > level_margin`` —
  :class:`~src.exit.zones.trailing_ratchet.TrailingRatchetStop`.

A :class:`~src.exit.base.CloseProfile` composes the three updaters: on each tick
it classifies the zone and delegates to the matching updater, which returns a new
stop level to ratchet to (always higher than the current one) or ``None`` to hold.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from src.feed.price_buffer import EpicBuffer


class StopZone(Enum):
    """Which price zone the live bid sits in (see module docstring)."""

    UNDERWATER = "underwater"
    BREAKEVEN_BAND = "breakeven_band"
    PROFIT = "profit"


def classify_zone(
    current_bid: float, level_zero: float, level_margin: float
) -> StopZone:
    """Classify the live bid into a :class:`StopZone`.

    ``UNDERWATER`` at or below break-even, ``BREAKEVEN_BAND`` in the noise band
    just above it, ``PROFIT`` once the bid clears the (open-frozen) margin level.
    """
    if current_bid > level_margin:
        return StopZone.PROFIT
    if current_bid > level_zero:
        return StopZone.BREAKEVEN_BAND
    return StopZone.UNDERWATER


@dataclass(slots=True)
class StopContext:
    """Everything a :class:`StopUpdater` needs to decide a new stop this tick.

    Assembled once per tick by the close profile from the live market state and
    the position's persisted levels, so the updaters stay pure and side-effect
    free.
    """

    current_bid: float
    level_open: float
    level_zero: float
    level_margin: float
    level_follower: float
    atr_value: float
    spread: float
    euro_per_point: float
    buf: EpicBuffer


class StopUpdater(ABC):
    """Decides the stop move for one price zone.

    Each updater is a named, independently-selectable strategy for its zone: the
    three zones are chosen separately in ``.env`` (``CLOSE_ZONESTART`` /
    ``CLOSE_ZONEMARGE`` / ``CLOSE_ZONEPROFIT``) and composed by
    :class:`~src.exit.close_zoneprofit.CloseZoneProfit`. Updaters are registered
    per zone in :mod:`src.exit.zones` and built through :func:`build_zone_updater`.
    """

    #: Registry key and per-zone ``CLOSE_ZONE*`` value (snake_case, stable).
    name: str = "base"

    @classmethod
    def from_settings(cls, settings) -> StopUpdater:
        """Build the updater from application settings.

        Updaters carry their shaping constants on their own class, so the default
        is a bare construction; override when an updater must read ``settings``.
        """
        return cls()

    @abstractmethod
    def propose(self, ctx: StopContext) -> float | None:
        """New absolute stop level to ratchet to (higher than the current follower),
        or ``None`` to hold the stop where it is this tick."""


def build_zone_updater(
    registry: dict[str, type[StopUpdater]], name: str, settings
) -> StopUpdater:
    """Build the zone updater registered under ``name`` from ``settings``.

    Raises:
        ValueError: when ``name`` is not registered for this zone.
    """
    cls = registry.get(name)
    if cls is None:
        raise ValueError(
            f"Unknown zone updater: {name!r} (available: {sorted(registry)})"
        )
    return cls.from_settings(settings)
