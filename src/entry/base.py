"""Entry domain — the *open* side of the decoupled trading pipeline.

An :class:`EntryStrategy` is the single decision point that turns a price
buffer into a decision to **open** a position. It is deliberately ignorant of
how that position will later be closed: it produces an :class:`EntryIntent`
carrying only the direction (and an optional sizing hint), *never* any exit
level. Everything about the exit — the protective stop, any take-profit, the
trailing behaviour — is owned independently by a :class:`~src.exit.base.CloseProfile`.

This is the core of the open/close decoupling: an entry idea and an exit
scenario are composed at runtime by the orchestration layer, can be swapped
independently, and can each be unit-tested on their own without the other.

Strategies are selected by name through the registry in :mod:`src.entry`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from src.feed.price_buffer import EpicBuffer


@dataclass(slots=True)
class EntryIntent:
    """A decision to open a position — and nothing about closing it.

    Attributes:
        epic: Market identifier the intent targets.
        direction: ``"BUY"`` or ``"SELL"``.
        size_hint: Multiplier applied by the sizing layer on top of the
            risk-based quantity (default ``1.0`` = no scaling). It is a *hint*,
            not a quantity: the execution layer remains free to bound it.
        score: Diagnostic confidence figure for logging/UI only (e.g. the
            efficiency ratio). It has no effect on the exit.
    """

    epic: str
    direction: str
    size_hint: float = 1.0
    score: float = 0.0


class EntryStrategy(ABC):
    """Entry-signal generator — the *open* decision, exit-agnostic."""

    #: Registry key and ``ENTRY_STRATEGY_NAME`` value (kebab/snake, stable).
    name: str = "base"

    @property
    @abstractmethod
    def warmup(self) -> int:
        """Minimum number of buffered candles required before evaluating."""

    @classmethod
    @abstractmethod
    def from_settings(cls, settings) -> EntryStrategy:
        """Build the strategy from application :class:`~src.core.config.Settings`."""

    @abstractmethod
    def evaluate(self, epic: str, buf: EpicBuffer) -> EntryIntent | None:
        """Evaluate the latest market state for one epic.

        Returns:
            An :class:`EntryIntent` (direction only, no exit levels) when a
            setup is present, or ``None`` to stay flat.
        """
