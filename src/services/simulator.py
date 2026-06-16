"""Strategy simulator — replays the project's trading rules on synthetic curves.

Feeds fictional candles (see :mod:`src.services.curve_generator`) through the
exact same decision pipeline used live:

- signal generation: any pluggable :class:`src.strategies.base.BaseStrategy`,
  selected by name (defaults to the ``STRATEGY_NAME`` setting)
- pre-open gates: :func:`src.services.trading.evaluate_open_gates`
- close rules: :func:`src.services.trading.decide_close_reason`
- ATR trailing stop: :func:`src.services.trading.compute_trailing_stop`

The simulator is deliberately blind to how the curves are produced: it only
consumes a ``curve_provider`` callable returning ``list[Candle]`` per
(day, epic). Broker behaviour is emulated minimally: a BUY fills at the offer,
the protective stop fills intra-candle when the bid low crosses it, and every
position still open at the end of a day is force-closed (end_of_day).

Everything runs in memory — no DB, no IG API. A full run takes seconds, which
is the point: estimate the coherence of the open/close rules without waiting
for a real trading day.
"""

import logging
import random
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from src.services.compute import atr
from src.services.price_buffer import Candle, EpicBuffer
from src.services.trading import (
    TradeConfig,
    compute_trailing_stop,
    decide_close_reason,
    evaluate_open_gates,
)
from src.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)

# Curves are stamped on arbitrary consecutive fictional days.
_BASE_DAY = datetime(2024, 1, 1)

CurveProvider = Callable[[int, int], list[Candle]]


@dataclass(slots=True)
class SimulationConfig:
    """Simulation run parameters (independent of the strategy itself)."""

    target_trades: int = 100  # stop once this many positions have closed
    max_days: int = 60  # hard cap on simulated days
    epics_per_day: int = 3  # independent fictional markets per day
    candles_per_day: int = 600  # 1-minute candles (07:00 → 16:59)
    profile: str = "random"  # curve generator profile
    seed: int | None = None  # master seed (None = different run each time)
    base_price: float = 8000.0
    euro_per_point: float = 1.0  # fictional contract value, € per point
    quantity: int = 1


@dataclass(slots=True)
class SimulatedTrade:
    """One simulated open/close cycle with the same levels as a live Position."""

    epic: str
    day: int
    open_time: str
    level_open: float
    level_win: float
    level_zero: float
    level_loose: float
    level_stop: float  # broker-side protective stop (trails upward)
    euro_stop: float
    close_time: str | None = None
    level_close: float | None = None
    reason_close: str | None = None
    euro: float | None = None
    win: bool = False
    stop_updates: int = 0
    # internal monitoring state (not part of the report)
    level_follower: float = 0.0


@dataclass
class SimulationResult:
    """Aggregated outcome of a simulation run."""

    trades: list[SimulatedTrade] = field(default_factory=list)
    days_simulated: int = 0
    buy_signals: int = 0
    rejections: Counter = field(default_factory=Counter)
    daily_pnl: list[float] = field(default_factory=list)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.win)

    @property
    def losses(self) -> int:
        return len(self.trades) - self.wins

    @property
    def total_pnl(self) -> float:
        return sum(t.euro or 0.0 for t in self.trades)

    def summary(self) -> dict:
        """Flat stats dictionary for the web API."""
        pnls = [t.euro or 0.0 for t in self.trades]
        win_pnls = [p for p in pnls if p > 0]
        loss_pnls = [p for p in pnls if p <= 0]

        equity: list[float] = []
        total = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for p in pnls:
            total += p
            peak = max(peak, total)
            max_drawdown = max(max_drawdown, peak - total)
            equity.append(round(total, 2))

        reasons = Counter(t.reason_close or "?" for t in self.trades)
        count = len(pnls)
        return {
            "trades": count,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.wins / count, 4) if count else 0.0,
            "total_pnl": round(total, 2),
            "avg_win": round(sum(win_pnls) / len(win_pnls), 2) if win_pnls else 0.0,
            "avg_loss": round(sum(loss_pnls) / len(loss_pnls), 2) if loss_pnls else 0.0,
            "best": round(max(pnls), 2) if pnls else 0.0,
            "worst": round(min(pnls), 2) if pnls else 0.0,
            "max_drawdown": round(max_drawdown, 2),
            "days_simulated": self.days_simulated,
            "buy_signals": self.buy_signals,
            "rejections": dict(self.rejections),
            "close_reasons": dict(reasons),
            "equity": equity,
            "daily_pnl": [round(p, 2) for p in self.daily_pnl],
            "avg_stop_updates": (
                round(sum(t.stop_updates for t in self.trades) / count, 1)
                if count
                else 0.0
            ),
        }


class StrategySimulator:
    """Replays the project's open/close rules over synthetic market days."""

    def __init__(
        self,
        trade_config: TradeConfig,
        strategy: BaseStrategy,
        sim_config: SimulationConfig,
        curve_provider: CurveProvider | None = None,
    ) -> None:
        self._config = trade_config
        self._strategy = strategy
        self._sim = sim_config
        # Only :meth:`run` (the synthetic path) needs a provider; the historical
        # backtester feeds pre-built days straight to :meth:`run_days`.
        self._curves = curve_provider

    def run(self) -> SimulationResult:
        """Simulate day after day until the trade target (or day cap) is hit."""
        if self._curves is None:
            raise RuntimeError(
                "run() needs a curve_provider; use run_days() for pre-built days"
            )

        def synthetic_days() -> Iterable[list[tuple[str, list[Candle]]]]:
            for day in range(self._sim.max_days):
                yield [
                    (f"SIM.{day}.{e}", self._curves(day, e))
                    for e in range(self._sim.epics_per_day)
                ]

        result = self.run_days(synthetic_days())
        logger.info(
            "Simulation done: %d trades over %d days, P&L=%.2f€",
            len(result.trades),
            result.days_simulated,
            result.total_pnl,
        )
        return result

    def run_days(
        self, days: Iterable[Sequence[tuple[str, list[Candle]]]]
    ) -> SimulationResult:
        """Replay pre-built trading days through the shared open/close pipeline.

        Each item is one trading day: a sequence of ``(epic, candles)`` pairs.
        Days are consumed in order until the trade target is reached or the
        input is exhausted. Daily gates (max trades, daily P&L, win rate) reset
        per day, exactly like the synthetic :meth:`run`. This is the entry point
        used by the historical backtester (see :mod:`src.services.backtester`).
        """
        result = SimulationResult()
        for day_index, day in enumerate(days):
            if len(result.trades) >= self._sim.target_trades:
                break
            curves = [(label, candles) for label, candles in day if candles]
            if not curves:
                continue
            day_start = len(result.trades)
            self._run_day(day_index, curves, result)
            result.days_simulated += 1
            result.daily_pnl.append(
                sum(t.euro or 0.0 for t in result.trades[day_start:])
            )
        return result

    def _run_day(
        self,
        day: int,
        curves: Sequence[tuple[str, list[Candle]]],
        result: SimulationResult,
    ) -> None:
        """Play one trading day: feed every epic's candles and apply the rules.

        Candles across epics are merged into a single timestamp-ordered event
        stream so misaligned real-market series (different start times and
        lengths) interleave correctly. For the synthetic simulator — where all
        curves share the same per-tick timestamps and lengths — sorting by
        ``(timestamp, epic_index)`` reproduces the original lockstep tick order
        exactly, so synthetic runs are unchanged.
        """
        buffers = [EpicBuffer(epic=label) for label, _ in curves]
        open_positions: dict[int, SimulatedTrade] = {}
        closed_today: list[SimulatedTrade] = []
        last_candles: list[Candle | None] = [None] * len(curves)

        events = sorted(
            (
                (candle.timestamp, e, candle)
                for e, (_, candles) in enumerate(curves)
                for candle in candles
            ),
            key=lambda ev: (ev[0], ev[1]),
        )

        prev_ts: datetime | None = None
        for ts, e, candle in events:
            # Re-check the early-stop boundary only between timestamps so a tick
            # is never processed half-way across epics (matches the old loop).
            if prev_ts is not None and ts != prev_ts:
                if len(result.trades) >= self._sim.target_trades and not open_positions:
                    break
            prev_ts = ts

            buffers[e].add(candle)
            last_candles[e] = candle

            position = open_positions.get(e)
            if position is not None:
                if self._monitor(position, candle, buffers[e]):
                    del open_positions[e]
                    closed_today.append(position)
                    result.trades.append(position)
            else:
                self._evaluate(
                    day, e, candle, buffers[e], open_positions, closed_today, result
                )

        # Force-close anything still open at the end of the day.
        for e, position in list(open_positions.items()):
            candle = last_candles[e]
            if candle is not None:
                self._close(position, candle, candle.bid_close, "end_of_day")
                result.trades.append(position)

    # ------------------------------------------------------------------ #
    # Opening                                                             #
    # ------------------------------------------------------------------ #

    def _evaluate(
        self,
        day: int,
        epic_index: int,
        candle: Candle,
        buf: EpicBuffer,
        open_positions: dict[int, SimulatedTrade],
        closed_today: list[SimulatedTrade],
        result: SimulationResult,
    ) -> None:
        """Mirror of the scheduler's ``_evaluate_epic`` + pre-open gates."""
        if len(buf) < self._strategy.warmup:
            return

        signal = self._strategy.evaluate(buf.epic, buf)
        if signal is None or signal.direction != "BUY":
            return
        result.buy_signals += 1

        trade_count = len(closed_today)
        wins = sum(1 for t in closed_today if t.win)
        win_rate = wins / trade_count if trade_count else 1.0
        daily_pnl = sum(t.euro or 0.0 for t in closed_today)

        allowed, reason = evaluate_open_gates(
            epic=buf.epic,
            direction=signal.direction,
            in_trading_hours=(
                self._config.hour_start <= candle.timestamp.hour < self._config.hour_end
            ),
            epic_already_open=epic_index in open_positions,
            open_count=len(open_positions),
            daily_pnl=daily_pnl,
            trade_count=trade_count,
            win_rate=win_rate,
            config=self._config,
        )
        if not allowed:
            # Normalize the reason (drop per-run numbers) for the counter.
            result.rejections[reason.split("(")[0].strip()] += 1
            return

        levels = signal.levels
        stop_level = levels.level_security
        stop_distance = levels.bid - stop_level
        euro_risk = stop_distance * self._sim.euro_per_point * self._sim.quantity
        if euro_risk > self._config.euro_loss_max:
            result.rejections["Euro risk too high"] += 1
            return

        # A market BUY fills at the offer (same as IG's confirmation level).
        open_positions[epic_index] = SimulatedTrade(
            epic=buf.epic,
            day=day,
            open_time=candle.timestamp.strftime("%H:%M"),
            level_open=round(candle.offer_close, 5),
            level_win=round(levels.level_win, 5),
            level_zero=round(levels.level_zero, 5),
            level_loose=round(levels.level_loose, 5),
            level_stop=round(stop_level, 5),
            level_follower=round(levels.level_follower, 5),
            euro_stop=round(euro_risk, 2),
        )

    # ------------------------------------------------------------------ #
    # Monitoring / closing                                                #
    # ------------------------------------------------------------------ #

    def _monitor(
        self, position: SimulatedTrade, candle: Candle, buf: EpicBuffer
    ) -> bool:
        """Mirror of ``TradingService.check_and_close`` + the broker-side stop.

        Returns True when the position was closed.
        """
        current_bid = candle.bid_close

        # Broker-side protective stop: fills intra-candle when the low touches
        # it (this is what IG does with the pushed stopLevel).
        broker_stop = max(position.level_stop, position.level_follower)
        if candle.bid_low <= broker_stop:
            reason = "follower" if position.stop_updates else "stop"
            self._close(position, candle, broker_stop, reason)
            return True

        reason = decide_close_reason(
            current_bid,
            level_win=position.level_win,
            level_loose=position.level_loose,
            is_close_hour=candle.timestamp.hour >= self._config.hour_close,
        )
        if reason is not None:
            self._close(position, candle, current_bid, reason)
            return True

        # Follower strategy: trail the stop with the same ATR logic as live.
        if (
            current_bid > position.level_open
            and self._config.close_strategy == "follower"
        ):
            new_stop = compute_trailing_stop(
                current_bid,
                atr_value=atr(list(buf.candles), self._config.atr_period),
                spread=candle.spread,
                level_zero=position.level_zero,
                level_follower=position.level_follower,
                euro_per_point=self._sim.euro_per_point * self._sim.quantity,
                euro_stop=position.euro_stop,
                config=self._config,
            )
            if new_stop is not None and new_stop > position.level_follower:
                position.level_follower = round(new_stop, 5)
                position.stop_updates += 1
        return False

    def _close(
        self,
        position: SimulatedTrade,
        candle: Candle,
        close_level: float,
        reason: str,
    ) -> None:
        """Record the close and the euro P&L (same formula as ``_euro_pnl``)."""
        move = close_level - position.level_open
        euro = move * self._sim.euro_per_point * self._sim.quantity
        position.close_time = candle.timestamp.strftime("%H:%M")
        position.level_close = round(close_level, 5)
        position.reason_close = reason
        position.euro = round(euro, 2)
        position.win = euro > 0


def run_simulation(
    settings,
    sim_config: SimulationConfig,
    strategy_name: str | None = None,
) -> SimulationResult:
    """Wire the curve generator to the simulator and run it.

    The provider derives one deterministic seed per (day, epic) from the master
    seed, so a run is fully reproducible while every curve stays distinct.
    The entry strategy is resolved by name through the registry; when
    ``strategy_name`` is omitted the configured ``STRATEGY_NAME`` is used —
    the simulator then replays exactly what the live bot would do.
    """
    from src.services.curve_generator import generate_curve
    from src.strategies import get_strategy

    master = random.Random(sim_config.seed)
    curve_seeds: dict[tuple[int, int], int] = {}

    def provider(day: int, epic_index: int) -> list[Candle]:
        key = (day, epic_index)
        if key not in curve_seeds:
            curve_seeds[key] = master.randrange(2**32)
        return generate_curve(
            sim_config.profile,
            seed=curve_seeds[key],
            num_candles=sim_config.candles_per_day,
            base_price=sim_config.base_price,
            day=_BASE_DAY + timedelta(days=day),
        )

    simulator = StrategySimulator(
        trade_config=TradeConfig.from_settings(settings),
        strategy=get_strategy(strategy_name or settings.strategy_name, settings),
        sim_config=sim_config,
        curve_provider=provider,
    )
    return simulator.run()
