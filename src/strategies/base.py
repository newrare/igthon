"""Pluggable strategy interface shared by the live bot and the simulator.

A *strategy* is the single decision point that turns a price buffer into an
entry signal. Everything else — scheduler jobs, the API queue, the trading
service (gates, order placement, trailing stop), the simulator, the dashboard —
is shared infrastructure that stays identical regardless of the chosen
strategy.

The contract is intentionally tiny:

- :attr:`BaseStrategy.warmup` — minimum candles needed before evaluating;
- :meth:`BaseStrategy.evaluate` — return a :class:`~src.core.indicators.TradingSignal`
  (direction + all position levels) or ``None`` to stay flat.

The signal reuses the existing :class:`TradingSignal` / ``TradingLevels``
dataclasses so the downstream pipeline (``evaluate_open_gates``,
``TradingService.open_position``, ``check_and_close``, the simulator's monitor
loop) needs no per-strategy code. A strategy with no fixed take-profit sets
``level_win = 0.0`` — ``decide_close_reason`` skips the win check then and the
position exits via the trailing stop only.

Strategies are selected **by name** through the registry in
:mod:`src.strategies` and the ``STRATEGY_NAME`` setting; each implementation
documents itself in ``docs/strategies/<name>.md``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.core.indicators import TradingSignal
from src.feed.price_buffer import EpicBuffer


class BaseStrategy(ABC):
    """Entry-signal generator plugged into the shared trading pipeline."""

    #: Registry key and ``STRATEGY_NAME`` value (kebab/snake, stable).
    name: str = "base"

    #: Whether opens are driven by an hourly cross-epic selection job rather than
    #: the per-epic 30s ``collect_analyze`` loop. The default (per-epic) keeps
    #: every existing strategy on the immediate-open path; a strategy that ranks
    #: all epics and opens only the single best one each hour sets this True so
    #: the scheduler skips the per-epic auto-open and runs ``trend_select`` instead.
    hourly_selection: bool = False

    @property
    @abstractmethod
    def warmup(self) -> int:
        """Minimum number of buffered candles required before evaluating."""

    @classmethod
    @abstractmethod
    def from_settings(cls, settings) -> BaseStrategy:
        """Build the strategy from application :class:`~src.core.config.Settings`."""

    @abstractmethod
    def evaluate(self, epic: str, buf: EpicBuffer) -> TradingSignal | None:
        """Evaluate the latest market state for one epic.

        Args:
            epic: Market identifier (used for logging/labels only).
            buf: Rolling candle buffer for the epic (oldest first).

        Returns:
            A complete ``TradingSignal`` (direction BUY/SELL/NEUTRAL plus the
            position levels), or ``None`` when the strategy stays flat —
            insufficient data, gate not passed, or simply no setup.
        """
