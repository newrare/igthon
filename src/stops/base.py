"""Stop-distance domain — where the *initial* protective stop is placed at open.

This is the third interchangeable trading decision, decoupled from both the
*open* side (:mod:`src.entry`) and the *close* side (:mod:`src.exit`) exactly
the same way they are decoupled from each other:

- an :class:`~src.entry.base.EntryStrategy` decides the **direction**;
- a :class:`StopDistance` decides **how far the initial protective stop sits
  from the entry** (the distance that drives risk-based sizing and the stop sent
  with the IG order);
- a :class:`~src.exit.base.CloseProfile` owns the **break-even / target
  references and every per-tick stop update** thereafter.

A close profile composes a stop distance at open (through the registry in
:mod:`src.stops`), so the initial-stop placement can be swapped —
``STOP_STRATEGY`` in ``.env`` — without touching the entry idea or the
exit management, and can be unit-tested on hand-built price paths on its own.

Distances are selected by name through the registry in :mod:`src.stops`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.feed.price_buffer import EpicBuffer


class StopDistance(ABC):
    """Chooses the absolute initial protective stop for a position about to open."""

    #: Registry key and ``STOP_STRATEGY`` value (snake_case, stable).
    name: str = "base"

    @classmethod
    @abstractmethod
    def from_settings(cls, settings) -> StopDistance:
        """Build the distance policy from application settings.

        Parameters are constants in each policy class, so most build from their
        own defaults and ignore ``settings``; the argument is kept for interface
        stability with the registry.
        """

    @abstractmethod
    def initial_stop(
        self, *, entry_level: float, direction: str, buf: EpicBuffer
    ) -> float:
        """Absolute initial protective stop level.

        Below ``entry_level`` for a BUY, above it for a SELL. This distance
        drives both the risk-based sizing and the stop attached to the IG order.
        """
