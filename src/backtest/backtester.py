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
from collections import Counter, defaultdict
from dataclasses import dataclass, fields

from src.backtest.contract_values import ContractTable
from src.backtest.simulator import (
    SimulationConfig,
    SimulationResult,
    StrategySimulator,
    direction_sign,
)
from src.entry import ENTRY_STRATEGIES, get_entry_strategy
from src.execution.trading import TradeConfig
from src.exit import get_close_profile
from src.exit.zones import (
    ZONEMARGE_UPDATERS,
    ZONEPROFIT_UPDATERS,
    ZONESECURE_UPDATERS,
    ZONESTART_UPDATERS,
)
from src.feed.price_buffer import Candle
from src.stops import STOP_DISTANCES

logger = logging.getLogger(__name__)

#: Trade cap meaning "no cap" — replay every archived day of the selection.
#: The synthetic simulator needs a trade target because it generates days
#: endlessly; a backtest reads a finite archive, so capping would only truncate
#: the data silently. Kept as a number (rather than ``None``) so the shared
#: replay engine's ``len(trades) >= target`` check needs no special case.
NO_TRADE_CAP = 1_000_000_000


@dataclass(slots=True)
class BacktestConfig:
    """Backtest run parameters (the historical analogue of SimulationConfig).

    Unlike the synthetic simulator there is no curve generation here, so the
    profile/seed/base-price knobs are gone; the candles come from the archive.
    """

    # Stop once this many positions have closed. Defaults to no cap: a backtest
    # replays the whole selected archive (see NO_TRADE_CAP).
    target_trades: int = NO_TRADE_CAP
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


#: The six ``.env`` selectors that define a run, mapped to the registry of valid
#: names for each. A backtest exists to compare these against one another, so the
#: page offers all six and the run applies them instead of the live configuration.
SELECTION_REGISTRIES: dict[str, dict] = {
    "open_strategy": ENTRY_STRATEGIES,
    "stop_strategy": STOP_DISTANCES,
    "close_zonestart": ZONESTART_UPDATERS,
    "close_zonemarge": ZONEMARGE_UPDATERS,
    "close_zonesecure": ZONESECURE_UPDATERS,
    "close_zoneprofit": ZONEPROFIT_UPDATERS,
}


@dataclass(slots=True)
class StrategySelection:
    """One run's choice of the six decoupled selectors, each optional.

    A field left at ``None`` falls back to the live ``.env`` value, so a run can
    override a single zone and leave everything else exactly as production has it.
    """

    open_strategy: str | None = None
    stop_strategy: str | None = None
    close_zonestart: str | None = None
    close_zonemarge: str | None = None
    close_zonesecure: str | None = None
    close_zoneprofit: str | None = None

    def problems(self, settings) -> dict[str, str]:
        """``{selector: why this run cannot be replayed}``, empty when it is clean.

        Validated on the **resolved** names — the explicit override *or* the live
        ``.env`` value it falls back to. A live value the offline engine cannot
        reproduce has to fail as loudly as one typed into the request, otherwise a
        plain "backtest this week" call would quietly replay something else under
        the live configuration's name.
        """
        bad: dict[str, str] = {}
        for selector, name in self.resolve(settings).items():
            if name not in SELECTION_REGISTRIES[selector]:
                bad[selector] = f"unknown name {name!r}"
                continue
            reason = untestable_reason(selector, name)
            if reason:
                bad[selector] = f"{name!r} is not backtestable — {reason}"
        return bad

    def apply(self, settings):
        """A read-through view of ``settings`` with the chosen selectors replaced.

        The composition layer resolves the exit from attributes on ``settings``
        (``CloseZoneProfit.from_settings`` reads ``stop_strategy`` and the four
        ``close_zone*`` names), and every strategy parameter is read from there
        too. Overlaying rather than rebuilding therefore swaps exactly the six
        decisions under test while keeping all tuning parameters intact — the
        backtest stays a faithful replay of the live pipeline.
        """
        overrides = {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if getattr(self, f.name) is not None
        }
        return _SettingsOverlay(settings, overrides) if overrides else settings

    def resolve(self, settings) -> dict[str, str]:
        """The effective name of each selector once ``settings`` fills the gaps."""
        effective = self.apply(settings)
        return {name: getattr(effective, name, "") for name in SELECTION_REGISTRIES}


#: ``selector → {name: why it is not backtestable}``. These names are valid live
#: configuration, but the offline engine cannot reproduce what they do, so the
#: backtest **refuses** them outright rather than replaying a degraded version
#: under their name. The page hides them (shown disabled when they are the live
#: value) and the API answers 400.
UNTESTABLE_NAMES: dict[str, dict[str, str]] = {
    "open_strategy": {
        "open_manual": "waits for a human order — there is no signal to replay",
        "open_testing": "opens unconditionally — replaying it tests nothing",
    },
    "close_zonestart": {
        "smartgroup": (
            "decides for the whole book from a cross-position pre-pass the "
            "scheduler runs (plan_group), which the replay engine has no "
            "equivalent of — a run would silently apply no group tightening"
        ),
    },
}


def untestable_reason(selector: str, name: str | None) -> str | None:
    """Why ``name`` cannot be backtested for ``selector``, or ``None`` when it can."""
    if not name:
        return None
    return UNTESTABLE_NAMES.get(selector, {}).get(name)


def backtestable_names(selector: str) -> list[str]:
    """Sorted names offered for ``selector``, minus the untestable ones."""
    excluded = UNTESTABLE_NAMES.get(selector, {})
    return sorted(set(SELECTION_REGISTRIES[selector]) - set(excluded))


class _SettingsOverlay:
    """Settings proxy: a handful of attributes replaced, everything else delegated.

    Kept deliberately dumb (no copying, no validation) so it works with the real
    pydantic ``Settings`` and with the ``SimpleNamespace`` stand-ins the tests use.
    Overrides live in the instance ``__dict__``, which Python consults before
    ``__getattr__``, so they win without any lookup logic here.
    """

    def __init__(self, base, overrides: dict[str, str]) -> None:
        self.__dict__["_base"] = base
        self.__dict__.update(overrides)

    def __getattr__(self, name: str):
        return getattr(self.__dict__["_base"], name)


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

    ``sign × (close - open) / open × 100``, signed by direction so a short's
    profit reads positive. Price-based and contract-agnostic, so it is directly
    comparable across instruments (a DAX index and a forex pair alike) without any
    contract value — which is what makes it the fallback lens for the epics the
    contract table cannot price.
    """
    open_level = float(trade.level_open or 0.0)
    if not open_level or trade.level_close is None:
        return 0.0
    move = direction_sign(getattr(trade, "direction", "BUY")) * (
        float(trade.level_close) - open_level
    )
    return move / open_level * 100.0


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


def _move(trade) -> float | None:
    """Profitable movement of a closed trade, in the instrument's own units.

    ``sign × (level_close - level_open)``: positive when the trade made money,
    whichever side it was on. Returns ``None`` for a trade with no usable fill
    pair.
    """
    open_level = float(trade.level_open or 0.0)
    if not open_level or trade.level_close is None:
        return None
    sign = direction_sign(getattr(trade, "direction", "BUY"))
    return sign * (float(trade.level_close) - open_level)


def trade_euro(trade, euro_per_point: float | None) -> float | None:
    """Euro P&L of one trade, or ``None`` when its epic has no € / point.

    ``move × euro_per_point`` — the same formula the live path applies to a real
    position (see ``TradingService._euro_pnl``), with the per-point value coming
    from the contract table instead of a ``/markets`` call.
    """
    move = _move(trade)
    if move is None or not euro_per_point:
        return None
    return move * euro_per_point


def _breakeven_move(trade) -> float | None:
    """Profitable movement under the *close at break-even crossing* scenario.

    The counterfactual: the moment the close-out price goes strictly past
    break-even the position is closed at that price, instead of being left to the
    close profile. A trade that never crossed break-even is untouched and keeps its
    real movement — the scenario only ever cuts a trade short, it never rescues a
    losing one. Signed by direction like :func:`_move`.
    """
    if trade.level_breakeven_exit is None:
        return _move(trade)
    open_level = float(trade.level_open or 0.0)
    if not open_level:
        return None
    sign = direction_sign(getattr(trade, "direction", "BUY"))
    return sign * (float(trade.level_breakeven_exit) - open_level)


def trade_euro_breakeven(trade, euro_per_point: float | None) -> float | None:
    """Euro P&L under the break-even-exit scenario (see :func:`_breakeven_move`)."""
    move = _breakeven_move(trade)
    if move is None or not euro_per_point:
        return None
    return move * euro_per_point


def _equity(values: list[float]) -> tuple[list[float], float]:
    """Running total of ``values`` plus its max peak-to-trough drawdown."""
    curve: list[float] = []
    total = peak = drawdown = 0.0
    for v in values:
        total += v
        peak = max(peak, total)
        drawdown = max(drawdown, peak - total)
        curve.append(round(total, 2))
    return curve, round(drawdown, 2)


def euro_summary(trades, table: ContractTable) -> dict:
    """Euro-denominated stats, real and under the break-even-exit scenario.

    Counts and win rates cover **every** trade (they need no contract value), but
    a euro total can only include the trades whose epic is in the contract table.
    ``unpriced_epics`` / ``unpriced_trades`` report what was left out, so a
    partial table shows up as a partial euro figure rather than a wrong one.

    The scenario counts (``wins_breakeven`` / ``losses_breakeven``) are derived
    from price levels, not from the euro figures, so they cover every trade just
    like the real counts do.
    """
    euros: list[float] = []
    euros_be: list[float] = []
    unpriced: Counter = Counter()
    crossed = 0

    for trade in trades:
        if trade.level_breakeven_exit is not None:
            crossed += 1
        epp = table.euro_per_point(trade.epic)
        if epp is None:
            unpriced[trade.epic] += 1
            continue
        euro = trade_euro(trade, epp)
        euro_be = trade_euro_breakeven(trade, epp)
        if euro is None or euro_be is None:
            continue
        euros.append(euro)
        euros_be.append(euro_be)

    # Scenario outcome per trade, on levels — independent of the contract table.
    be_moves = [_breakeven_move(t) or 0.0 for t in trades if t.level_open]
    equity, drawdown = _equity(euros)
    equity_be, drawdown_be = _equity(euros_be)

    return {
        "priced_trades": len(euros),
        "unpriced_trades": sum(unpriced.values()),
        "unpriced_epics": sorted(unpriced),
        "contract_table_size": len(table),
        "contract_table_generated_at": table.generated_at,
        "total_euro": round(sum(euros), 2),
        "best_euro": round(max(euros), 2) if euros else 0.0,
        "worst_euro": round(min(euros), 2) if euros else 0.0,
        "max_drawdown_euro": drawdown,
        "equity_euro": equity,
        # Break-even-exit scenario.
        "breakeven_crossed": crossed,
        "total_euro_breakeven": round(sum(euros_be), 2),
        "wins_breakeven": sum(1 for m in be_moves if m > 0),
        "losses_breakeven": sum(1 for m in be_moves if m <= 0),
        "max_drawdown_euro_breakeven": drawdown_be,
        "equity_euro_breakeven": equity_be,
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
    selection: StrategySelection | None = None,
) -> SimulationResult:
    """Replay one full open/stop/close selection over archived candles.

    ``selection`` overrides any of the six decoupled selectors for this run
    (``OPEN_STRATEGY``, ``STOP_STRATEGY`` and the four ``CLOSE_ZONE*``); anything
    it leaves unset falls back to the live ``.env`` value. The overridden settings
    are then handed to the very same factories the bot uses at startup, so the
    replay is the configuration under test — not the live one, and not a
    hand-assembled approximation of it.
    """
    effective = (selection or StrategySelection()).apply(settings)
    kept, dropped = dedupe_correlated_epics(candles_by_epic)
    if dropped:
        logger.info(
            "Backtest deduped %d correlated epic(s): %s",
            len(dropped),
            ", ".join(dropped),
        )
    days = build_days(kept)
    simulator = StrategySimulator(
        trade_config=TradeConfig.from_settings(effective),
        entry=get_entry_strategy(effective.open_strategy, effective),
        close_profile=get_close_profile(effective),
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
