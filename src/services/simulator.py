"""Strategy simulator — replays the project's trading rules on synthetic curves.

Feeds fictional candles (see :mod:`src.services.curve_generator`) through the
exact same decision functions used live:

- signal generation: :func:`src.services.compute.compute_signal`
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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from src.services.compute import atr, compute_signal
from src.services.price_buffer import Candle, EpicBuffer
from src.services.trading import (
    TradeConfig,
    compute_breakeven_stop,
    compute_trailing_stop,
    decide_close_reason,
    evaluate_open_gates,
)

logger = logging.getLogger(__name__)

# Curves are stamped on arbitrary consecutive fictional days.
_BASE_DAY = datetime(2024, 1, 1)

CurveProvider = Callable[[int, int], list[Candle]]


@dataclass(slots=True)
class StrategyParams:
    """Signal-generation parameters, mirroring the scheduler's kwargs mapping."""

    regression_period: int = 20
    sma_fast_period: int = 5
    sma_slow_period: int = 20
    roc_period: int = 10
    min_r2: float = 0.70
    min_score: float = 0.75
    max_spread_ratio: float = 0.0015
    stop_multiplier: float = 2.5
    target_multiplier: float = 4.0
    tactic: str = "spread"

    @classmethod
    def from_settings(cls, settings) -> "StrategyParams":
        """Build from application Settings (same mapping as the scheduler)."""
        return cls(
            regression_period=settings.strategy_lookback_points,
            sma_fast_period=settings.strategy_sma_fast,
            sma_slow_period=settings.strategy_sma_slow,
            roc_period=settings.strategy_roc_period,
            min_r2=settings.strategy_min_r2,
            min_score=settings.strategy_min_score,
            max_spread_ratio=settings.strategy_max_spread_ratio,
            stop_multiplier=settings.strategy_stop_multiplier,
            target_multiplier=settings.strategy_target_multiplier,
            tactic=settings.strategy_tactic,
        )


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
    # Break-even lock (A/B toggle, off by default so the baseline is unchanged):
    # once price is a safe margin above level_zero, pull the stop just above zero
    # so the trade can no longer lose — independent of the ATR trailing ratchet.
    breakeven_lock: bool = False
    breakeven_buffer_mult: float = 1.0  # lock at level_zero + buffer × spread
    breakeven_margin_mult: float = 2.0  # min bid→stop gap to keep, in spreads
    ig_min_stop_distance: float = 0.0  # IG min stop distance (price units)


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
        curve_provider: CurveProvider,
        trade_config: TradeConfig,
        params: StrategyParams,
        sim_config: SimulationConfig,
    ) -> None:
        self._curves = curve_provider
        self._config = trade_config
        self._params = params
        self._sim = sim_config

    def run(self) -> SimulationResult:
        """Simulate day after day until the trade target (or day cap) is hit."""
        result = SimulationResult()
        for day in range(self._sim.max_days):
            if len(result.trades) >= self._sim.target_trades:
                break
            day_start = len(result.trades)
            self._run_day(day, result)
            result.days_simulated += 1
            result.daily_pnl.append(
                sum(t.euro or 0.0 for t in result.trades[day_start:])
            )
        logger.info(
            "Simulation done: %d trades over %d days, P&L=%.2f€",
            len(result.trades),
            result.days_simulated,
            result.total_pnl,
        )
        return result

    def _run_day(self, day: int, result: SimulationResult) -> None:
        """Play one fictional day: feed candles tick by tick and apply rules."""
        curves = [self._curves(day, e) for e in range(self._sim.epics_per_day)]
        buffers = [
            EpicBuffer(epic=f"SIM.{day}.{e}") for e in range(self._sim.epics_per_day)
        ]
        open_positions: dict[int, SimulatedTrade] = {}
        closed_today: list[SimulatedTrade] = []

        num_ticks = min(len(c) for c in curves) if curves else 0
        last_candles: list[Candle | None] = [None] * len(curves)

        for tick in range(num_ticks):
            for e, curve in enumerate(curves):
                candle = curve[tick]
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
                        e, candle, buffers[e], open_positions, closed_today, result
                    )

            if len(result.trades) >= self._sim.target_trades and not open_positions:
                break

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
        epic_index: int,
        candle: Candle,
        buf: EpicBuffer,
        open_positions: dict[int, SimulatedTrade],
        closed_today: list[SimulatedTrade],
        result: SimulationResult,
    ) -> None:
        """Mirror of the scheduler's ``_evaluate_epic`` + pre-open gates."""
        p = self._params
        if len(buf) < p.sma_slow_period:
            return

        signal = compute_signal(
            buf.epic,
            buf,
            regression_period=p.regression_period,
            sma_fast_period=p.sma_fast_period,
            sma_slow_period=p.sma_slow_period,
            roc_period=p.roc_period,
            min_r2=p.min_r2,
            min_score=p.min_score,
            max_spread_ratio=p.max_spread_ratio,
            follower_mult=p.stop_multiplier,
            win_mult=p.target_multiplier,
            loose_mult=p.stop_multiplier * 3,
            security_mult=p.stop_multiplier * 2,
            tactic=p.tactic,
        )
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
            day=int(buf.epic.split(".")[1]),
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
            # Break-even lock first (ATR-independent): make the trade risk-free as
            # soon as price is a safe margin above zero, even before the ATR trail
            # would fire or while the ATR is not yet computable.
            if self._sim.breakeven_lock:
                be_stop = compute_breakeven_stop(
                    current_bid,
                    level_zero=position.level_zero,
                    current_stop=max(position.level_stop, position.level_follower),
                    spread=candle.spread,
                    min_stop_distance=self._sim.ig_min_stop_distance,
                    buffer_mult=self._sim.breakeven_buffer_mult,
                    margin_mult=self._sim.breakeven_margin_mult,
                )
                if be_stop is not None and be_stop > position.level_follower:
                    position.level_follower = round(be_stop, 5)
                    position.stop_updates += 1

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
) -> SimulationResult:
    """Wire the curve generator to the simulator and run it.

    The provider derives one deterministic seed per (day, epic) from the master
    seed, so a run is fully reproducible while every curve stays distinct.
    """
    from src.services.curve_generator import generate_curve

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
        curve_provider=provider,
        trade_config=TradeConfig.from_settings(settings),
        params=StrategyParams.from_settings(settings),
        sim_config=sim_config,
    )
    return simulator.run()
