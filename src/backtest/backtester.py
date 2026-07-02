"""Historical backtester — replay a strategy on archived real-market candles.

This is the real-data counterpart of :mod:`src.backtest.simulator`. Where the
simulator feeds synthetic curves, the backtester feeds candles read from the
on-disk archive (:mod:`src.backtest.backtest_archive`). Both share the very same
replay engine (:class:`~src.backtest.simulator.StrategySimulator`), so a backtest
exercises the exact open/close rules the live bot uses: pluggable entry signal,
pre-open gates, win/stop levels and the ATR trailing stop.

Pipeline::

    BacktestArchive.load()  ->  build_days()  ->  StrategySimulator.run_days()

``build_days`` turns the per-epic candle series into the simulator's notion of a
trading day: candles are grouped by calendar date, and each date becomes one day
holding every epic that traded it, mirroring the live scheduler's per-day reset.

Everything runs in memory and reads only files — no DB session, no IG API — so a
backtest is safe to run while the main process keeps recording the current week.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

from src.backtest.simulator import SimulationConfig, SimulationResult, StrategySimulator
from src.entry import get_entry_strategy
from src.execution.trading import TradeConfig
from src.exit import get_close_profile
from src.feed.price_buffer import Candle

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BacktestConfig:
    """Backtest run parameters (the historical analogue of SimulationConfig).

    Unlike the synthetic simulator there is no curve generation here, so the
    profile/seed/base-price knobs are gone; the candles come from the archive.
    """

    target_trades: int = 100  # stop once this many positions have closed
    euro_per_point: float = 1.0  # contract value, € per point
    quantity: int = 1

    def to_simulation_config(self) -> SimulationConfig:
        """Project onto the SimulationConfig fields the replay engine reads.

        The synthetic-only fields (profile, seed, candles_per_day, base_price,
        epics_per_day, max_days) are irrelevant on the ``run_days`` path and keep
        their defaults.
        """
        return SimulationConfig(
            target_trades=self.target_trades,
            euro_per_point=self.euro_per_point,
            quantity=self.quantity,
        )


def _underlying(epic: str) -> str:
    """Underlying instrument key of an IG epic, e.g. ``IX.D.DAX.IDF.IP`` -> ``DAX``.

    IG lists the same market under several contracts (``IX.D.DAX.IDF.IP``,
    ``IX.D.DAX.IFMM.IP``, ``IX.D.DAX.IMF.IP`` are all the DAX; ``CS.D.EURUSD.CEF``
    and ``…CEFM`` are both EUR/USD). They share the third dotted field, which we
    use to group them.
    """
    parts = epic.split(".")
    return parts[2] if len(parts) >= 4 else epic


def dedupe_correlated_epics(
    candles_by_epic: dict[str, list[Candle]],
) -> tuple[dict[str, list[Candle]], list[str]]:
    """Collapse correlated duplicate contracts to one epic per underlying.

    Several IG contracts of the same instrument (the 3 DAX contracts, the 3 FTSE
    contracts, …) produce near-identical candles and therefore identical trades,
    which would triple-count the same bet in a backtest. For each underlying this
    keeps the epic with the most candles (the richest series; ties broken by
    name) and drops the rest. Returns ``(kept, dropped_epics)``.
    """
    groups: dict[str, list[tuple[str, list[Candle]]]] = {}
    for epic, candles in candles_by_epic.items():
        groups.setdefault(_underlying(epic), []).append((epic, candles))

    kept: dict[str, list[Candle]] = {}
    dropped: list[str] = []
    for members in groups.values():
        members.sort(key=lambda ec: (-len(ec[1]), ec[0]))
        keep_epic, keep_candles = members[0]
        kept[keep_epic] = keep_candles
        dropped.extend(epic for epic, _ in members[1:])
    return dict(sorted(kept.items())), sorted(dropped)


def trade_return_pct(trade) -> float:
    """Percentage return of one trade, computed from the actual fill prices.

    ``(close - open) / open * 100``. Price-based and contract-agnostic, so it is
    directly comparable across instruments (a DAX index and a forex pair alike)
    without any fabricated euro-per-point — which is exactly why the backtest
    reports returns rather than euros: the archive holds prices, not contract
    sizes or currency conversions.
    """
    open_level = float(trade.level_open or 0.0)
    if not open_level or trade.level_close is None:
        return 0.0
    return (float(trade.level_close) - open_level) / open_level * 100.0


def percentage_summary(trades) -> dict:
    """Aggregate per-trade percentage returns into backtest summary stats.

    Mirrors the shape of :meth:`SimulationResult.summary` but every magnitude is
    a percentage of entry price instead of a euro figure. The equity curve is the
    running sum of per-trade returns (in percentage points).
    """
    returns = [trade_return_pct(t) for t in trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]

    equity: list[float] = []
    total = peak = max_drawdown = 0.0
    for r in returns:
        total += r
        peak = max(peak, total)
        max_drawdown = max(max_drawdown, peak - total)
        equity.append(round(total, 4))

    return {
        "total_return_pct": round(total, 4),
        "avg_win_pct": round(sum(wins) / len(wins), 4) if wins else 0.0,
        "avg_loss_pct": round(sum(losses) / len(losses), 4) if losses else 0.0,
        "best_pct": round(max(returns), 4) if returns else 0.0,
        "worst_pct": round(min(returns), 4) if returns else 0.0,
        "max_drawdown_pct": round(max_drawdown, 4),
        "equity_pct": equity,
    }


def build_days(
    candles_by_epic: dict[str, list[Candle]],
) -> list[list[tuple[str, list[Candle]]]]:
    """Group per-epic candle series into chronological trading days.

    Each output item is one calendar date: a list of ``(epic, candles)`` pairs
    for every epic that has candles that date, each sub-series sorted oldest to
    newest. Days themselves are returned in chronological order. An epic absent
    from a given date simply does not appear in that day's list.
    """
    # date -> epic -> candles
    by_date: dict[object, dict[str, list[Candle]]] = defaultdict(dict)
    for epic, candles in candles_by_epic.items():
        per_date: dict[object, list[Candle]] = defaultdict(list)
        for candle in candles:
            per_date[candle.timestamp.date()].append(candle)
        for day, day_candles in per_date.items():
            day_candles.sort(key=lambda c: c.timestamp)
            by_date[day][epic] = day_candles

    days: list[list[tuple[str, list[Candle]]]] = []
    for day in sorted(by_date):
        epics = by_date[day]
        days.append([(epic, epics[epic]) for epic in sorted(epics)])
    return days


def run_backtest(
    settings,
    candles_by_epic: dict[str, list[Candle]],
    config: BacktestConfig,
    strategy_name: str | None = None,
    close_profile_name: str | None = None,
) -> SimulationResult:
    """Replay an entry strategy + close profile over archived candles.

    The entry strategy is resolved by name (``strategy_name`` or the configured
    ``OPEN_STRATEGY``). The exit is the single composer profile built from settings
    — its per-zone behaviour comes from the ``CLOSE_ZONE*`` selectors, so
    ``close_profile_name`` is accepted for API compatibility but not used for
    selection. The backtest thus replays exactly what the live bot would do.
    """
    kept, dropped = dedupe_correlated_epics(candles_by_epic)
    if dropped:
        logger.info(
            "Backtest deduped %d correlated epic(s): %s",
            len(dropped),
            ", ".join(dropped),
        )
    days = build_days(kept)
    simulator = StrategySimulator(
        trade_config=TradeConfig.from_settings(settings),
        entry=get_entry_strategy(strategy_name or settings.open_strategy, settings),
        close_profile=get_close_profile(settings),
        sim_config=config.to_simulation_config(),
    )
    result = simulator.run_days(days)
    logger.info(
        "Backtest done: %d trades over %d days (%d epics), P&L=%.2f€",
        len(result.trades),
        result.days_simulated,
        len(kept),
        result.total_pnl,
    )
    return result
