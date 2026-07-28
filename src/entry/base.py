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

    #: Registry key and ``OPEN_STRATEGY`` value (snake_case, stable).
    name: str = "base"

    #: Whether this strategy is allowed to emit ``SELL`` intents on the
    #: automatic path. The default is long-only: the orchestration layer drops a
    #: SELL intent from a strategy that has not opted in, and the shared pre-open
    #: gate (:func:`~src.execution.gates.evaluate_open_gates`) refuses it too, so
    #: a long-only entry cannot short by accident. A strategy that genuinely
    #: trades both ways sets this to True; the scheduler then keeps its SELL
    #: intents and passes ``allow_short`` down to the gate. Manual dashboard
    #: opens are unaffected — they carry their own ``allow_short``.
    emits_shorts: bool = False

    #: How the orchestration layer drives this strategy. The default per-epic
    #: entries are evaluated by the 30-second analysis loop, which opens on every
    #: BUY intent. A ``cross_epic_selection`` entry is a *ranker* instead:
    #: :meth:`evaluate` always returns a comparable ``score`` for ranking, the
    #: per-epic auto-open loop leaves it alone, and the scheduler scores all
    #: tradable epics, ranks them and opens the best one(s) to maintain a target
    #: number of open positions. See
    #: :class:`~src.entry.open_ranking.OpenRanking`.
    cross_epic_selection: bool = False

    #: Rolling cross-epic selection knobs — consulted by the scheduler only when
    #: ``cross_epic_selection`` is True. These defaults define the contract; a
    #: ranker sets its own values as constants in its own class. They are
    #: deliberately *not* loaded from settings/``.env``: strategy parameters live
    #: as constants in the strategy class, and the strategy is chosen at runtime
    #: from the dashboard.
    concurrent_positions: int = 1  # target number of open positions to hold
    # Wallet-bounded selection. When False (default) the selector holds exactly
    # ``concurrent_positions`` open positions. When True the fixed count target is
    # dropped: the selector keeps opening the best-ranked affordable epics until
    # the spendable balance (available funds minus ``wallet_reserve``) can no
    # longer cover another margin. ``concurrent_positions`` then serves only as a
    # conservative fallback cap for the pass when the balance cannot be read.
    wallet_bounded: bool = False
    # Legacy warm-up delay: no longer enforced live (the per-epic ``warmup``
    # candle count is the warm-up, and live opens are gated by ``marketStatus``
    # rather than a wall clock). Kept for reference / the simulator's own gating.
    open_after_minutes: int = 60
    wallet_reserve: float = 0.10  # fraction of available funds kept free
    # Minimum fraction of the livestreamed tradable universe that must be
    # warmed up (``len(buf) >= warmup``) before a winner is declared. Guards
    # against "false tournaments" — e.g. just after a mid-session restart only a
    # handful of epics have rebuilt enough history, so the ranker would crown the
    # least-bad of a tiny pool instead of the best of the day. ``0.5`` = strictly
    # more than half the universe must be ready to participate.
    min_participation_ratio: float = 0.5
    # NOTE: the same-day re-open policy is NOT a strategy knob. It is global to
    # every open strategy and read from ``ALLOW_SAME_DAY_REOPEN`` in .env: False
    # = one opening per epic per day (BUY or SELL), True = an epic is a candidate
    # again as soon as it holds no open position. It is enforced both by the
    # rolling selector's candidate filter and by the shared pre-open gate
    # (:func:`~src.execution.gates.evaluate_open_gates`), so per-epic strategies
    # obey it too. Concurrent duplicates on a still-open epic are always blocked
    # by ``epic_already_open``, whatever the policy.
    # Minimum minutes the rolling selector must wait between two opens. 0
    # (default) means no cooldown — the wallet-bounded selector may open several
    # positions in a single pass. When > 0 the selector opens at most one
    # position per pass and only once at least this many minutes have elapsed
    # since the most recent open, so positions are spaced out instead of fired in
    # a burst.
    open_cooldown_minutes: int = 0

    @property
    @abstractmethod
    def warmup(self) -> int:
        """Minimum number of buffered candles required before evaluating."""

    @classmethod
    @abstractmethod
    def from_settings(cls, settings) -> EntryStrategy:
        """Build the strategy.

        Parameters are constants in each strategy class, so most strategies build
        from their own defaults and ignore ``settings``; the argument is kept for
        the few entries that still read shared infra knobs and for interface
        stability with the registry.
        """

    @abstractmethod
    def evaluate(self, epic: str, buf: EpicBuffer) -> EntryIntent | None:
        """Evaluate the latest market state for one epic.

        Returns:
            An :class:`EntryIntent` (direction only, no exit levels) when a
            setup is present, or ``None`` to stay flat.
        """
