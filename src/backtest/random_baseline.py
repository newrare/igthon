"""Random-direction baseline — does an entry's choice of side beat a coin flip?

Every entry strategy is otherwise judged against zero, one at a time. That
cannot tell an entry that *picks the right side* from an entry that merely
opens while the close profile does all the work: the exit (stop, break-even,
trailing) shapes the payoff of any open, skilled or not. The honest reference is
the same replay with the skill removed.

:class:`DirectionShuffle` wraps a strategy and keeps **everything** it decides —
which epic, which minute, which score in the ranking, every class knob the
scheduler reads — except the side, which it replaces with a coin flip. Replaying
it over ``N`` seeds gives the null distribution of the strategy's own metric;
the strategy is worth something only when its real result sits in the upper
tail of that distribution (:func:`empirical_p_value`).

The flip is a pure function of ``(seed, epic, candle timestamp)``, not a stream
of random draws: a ranker re-evaluates every epic on every tick, so the same
moment must always get the same side whatever the evaluation order, and a seed
must reproduce its run exactly.

The baseline isolates **direction** skill. Timing and market selection are kept
identical on purpose (they are what the research dataset measures, see
:mod:`src.backtest.research`). Because the replayed trades take other paths, the
later opens of a day can differ slightly from the real run (a position that
closes earlier frees its slot earlier); the trade counts are reported side by
side so that drift stays visible.
"""

from __future__ import annotations

import hashlib
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, replace

from src.backtest.backtester import BacktestConfig, StrategySelection, run_backtest
from src.backtest.contract_values import ContractTable
from src.backtest.simulator import SimulatedTrade, direction_sign
from src.entry.base import EntryIntent, EntryStrategy
from src.feed.price_buffer import Candle, EpicBuffer


def coin_flip(seed: int, epic: str, stamp: str) -> str:
    """Deterministic side for ``(seed, epic, stamp)``: ``"BUY"`` or ``"SELL"``.

    Hash-based (stable across processes, unlike :func:`hash`), so the same
    moment always draws the same side within a seed and two seeds are
    independent.
    """
    digest = hashlib.blake2s(f"{seed}|{epic}|{stamp}".encode(), digest_size=1)
    return "BUY" if digest.digest()[0] & 1 else "SELL"


class DirectionShuffle:
    """Entry wrapper: same opens as ``inner``, side drawn by a coin flip.

    Duck-typed rather than an :class:`EntryStrategy` subclass so that **every**
    attribute the replay engine reads (``warmup``, ``cross_epic_selection``,
    ``open_cooldown_minutes``, ``filter_ranked``…) resolves to the wrapped
    strategy's own value — a subclass would shadow them with the base defaults.
    Only three names are its own: ``name``, ``emits_shorts`` (always ``True``: a
    coin flip draws SELL half the time, also for a long-only strategy) and
    :meth:`evaluate`.
    """

    def __init__(self, inner: EntryStrategy, seed: int) -> None:
        self._inner = inner
        self._seed = seed
        self.name = f"{inner.name}~random{seed}"
        self.emits_shorts = True

    def __getattr__(self, attribute: str):
        # Only reached for names not set in __init__: delegate to the strategy.
        return getattr(self._inner, attribute)

    def evaluate(self, epic: str, buf: EpicBuffer) -> EntryIntent | None:
        intent = self._inner.evaluate(epic, buf)
        if intent is None or not buf.candles:
            return intent
        stamp = buf.candles[-1].timestamp.isoformat()
        return replace(intent, direction=coin_flip(self._seed, epic, stamp))


def trade_r(trade: SimulatedTrade) -> float | None:
    """Result of a replayed trade in R (move ÷ fill-to-stop risk).

    The replay prices at 1 € per point, so ``euro_stop`` *is* the risk distance
    in price points. ``None`` when that distance rounded to zero (a forex point
    distance below the cent), which cannot be expressed in R.
    """
    if trade.level_close is None or trade.euro_stop <= 0:
        return None
    move = direction_sign(trade.direction) * (trade.level_close - trade.level_open)
    return move / trade.euro_stop


def mean_r(trades: Sequence[SimulatedTrade]) -> float:
    """Mean R over the trades that can be expressed in R (0.0 when none)."""
    values = [r for r in (trade_r(t) for t in trades) if r is not None]
    return statistics.fmean(values) if values else 0.0


def total_euro(trades: Sequence[SimulatedTrade], table: ContractTable) -> float:
    """Euro total of the trades whose epic the contract table prices."""
    total = 0.0
    for trade in trades:
        epp = table.euro_per_point(trade.epic)
        if epp is None or trade.level_close is None:
            continue
        sign = direction_sign(trade.direction)
        total += sign * (trade.level_close - trade.level_open) * epp
    return total


def replay(
    settings,
    candles_by_epic: dict[str, list[Candle]],
    selection: StrategySelection,
    table: ContractTable,
    *,
    seed: int | None,
) -> tuple[int, float, float]:
    """One replay — the real strategy (``seed=None``) or one coin-flip seed.

    Returns ``(trade count, mean R, euro total)``.
    """
    wrap = None if seed is None else (lambda entry: DirectionShuffle(entry, seed))
    result = run_backtest(
        settings, candles_by_epic, BacktestConfig(), selection, wrap_entry=wrap
    )
    return len(result.trades), mean_r(result.trades), total_euro(result.trades, table)


def empirical_p_value(actual: float, null: Sequence[float]) -> float:
    """One-sided p-value of ``actual`` against a null distribution.

    Share of null draws at least as good as ``actual``, with the usual ``+1``
    correction (the real run counts as one draw of the null), so the result is
    never 0 and ``N`` seeds can at best prove ``p = 1 / (N + 1)``.
    """
    if not null:
        return float("nan")
    at_least = sum(1 for value in null if value >= actual)
    return (at_least + 1) / (len(null) + 1)


def normal_p_value(actual: float, null: Sequence[float]) -> tuple[float, float]:
    """``(z, p)`` of ``actual`` against a null summarised as a normal law.

    The empirical p-value bottoms out at ``1 / (N + 1)``, so with 20 seeds it can
    never clear a Bonferroni threshold across a dozen strategies. A mean R over
    hundreds of trades is close to normal (central limit), so the null's own
    mean and standard deviation give a usable z-score and one-sided p beyond the
    seeds actually drawn. ``(nan, nan)`` for fewer than two draws or a constant
    null.
    """
    if len(null) < 2:
        return float("nan"), float("nan")
    sd = statistics.stdev(null)
    if sd <= 0:
        return float("nan"), float("nan")
    z = (actual - statistics.fmean(null)) / sd
    return z, 0.5 * math.erfc(z / math.sqrt(2))


@dataclass(slots=True)
class BaselineReport:
    """Real run of a strategy against its random-direction replays.

    Attributes:
        strategy: Entry strategy name.
        trades: Trade count of the real run.
        mean_r: Mean R of the real run.
        euro: Total euros of the real run (priced trades only).
        null_trades: Trade count of each random replay.
        null_mean_r: Mean R of each random replay — the null distribution.
        null_euro: Total euros of each random replay.
    """

    strategy: str
    trades: int
    mean_r: float
    euro: float
    null_trades: list[int]
    null_mean_r: list[float]
    null_euro: list[float]

    @property
    def p_value_r(self) -> float:
        """Chance a coin flip does at least as well as the strategy, in R."""
        return empirical_p_value(self.mean_r, self.null_mean_r)

    @property
    def z_r(self) -> float:
        """Distance of the strategy from chance, in null standard deviations."""
        return normal_p_value(self.mean_r, self.null_mean_r)[0]

    @property
    def p_value_r_normal(self) -> float:
        """Normal-approximation p-value in R (see :func:`normal_p_value`)."""
        return normal_p_value(self.mean_r, self.null_mean_r)[1]

    @property
    def p_value_euro(self) -> float:
        """Same test on the euro total."""
        return empirical_p_value(self.euro, self.null_euro)

    def summary(self) -> dict:
        """Flat dict for printing or JSON."""
        null_r = sorted(self.null_mean_r)
        return {
            "strategy": self.strategy,
            "trades": self.trades,
            "mean_r": self.mean_r,
            "euro": self.euro,
            "seeds": len(null_r),
            "null_trades_median": (
                statistics.median(self.null_trades) if self.null_trades else 0
            ),
            "null_mean_r_median": statistics.median(null_r) if null_r else 0.0,
            "null_mean_r_p95": null_r[int(0.95 * (len(null_r) - 1))] if null_r else 0.0,
            "null_euro_median": (
                statistics.median(self.null_euro) if self.null_euro else 0.0
            ),
            "p_value_r": self.p_value_r,
            "z_r": self.z_r,
            "p_value_r_normal": self.p_value_r_normal,
            "p_value_euro": self.p_value_euro,
        }
