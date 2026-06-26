"""Strategy simulator — replays the project's trading rules on synthetic curves.

Feeds fictional candles (see :mod:`src.backtest.curve_generator`) through the
exact same decoupled decision pipeline used live:

- open: an :class:`src.entry.base.EntryStrategy` produces a direction-only
  :class:`~src.entry.base.EntryIntent`;
- pre-open gates: :func:`src.execution.risk.evaluate_open_gates`;
- exit: a :class:`src.exit.base.CloseProfile` chosen *independently* picks the
  initial stop (``initial_plan``) and drives every per-tick close decision
  (``evaluate``).

Because open and close are decoupled, the simulator can pair any entry with any
close profile — so a close profile's behaviour can be measured on its own.

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

from src.entry.base import EntryStrategy
from src.execution.risk import evaluate_open_gates
from src.execution.trading import TradeConfig
from src.exit.base import ACTION_CLOSE, ACTION_UPDATE_STOP, CloseProfile
from src.feed.price_buffer import Candle, EpicBuffer

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
    level_margin: float = 0.0  # margin level frozen at open (read by the profile)
    euro_per_point: float = 0.0  # € per point of movement (read by the profile)
    close_time: str | None = None
    level_close: float | None = None
    reason_close: str | None = None
    euro: float | None = None
    win: bool = False
    stop_updates: int = 0
    # internal monitoring state (not part of the report)
    level_follower: float = 0.0
    opened_at: datetime | None = None  # open timestamp (read by trend-aware exits)


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
        entry: EntryStrategy,
        close_profile: CloseProfile,
        sim_config: SimulationConfig,
        curve_provider: CurveProvider | None = None,
    ) -> None:
        self._config = trade_config
        self._entry = entry
        self._close_profile = close_profile
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
        used by the historical backtester (see :mod:`src.backtest.backtester`).
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
        """Per-epic open path — mirror of the scheduler's ``_evaluate_epic``.

        The entry strategy decides direction only; the close profile picks the
        stop in :meth:`_try_open`.
        """
        if len(buf) < self._entry.warmup:
            return

        intent = self._entry.evaluate(buf.epic, buf)
        if intent is None or intent.direction != "BUY":
            return
        result.buy_signals += 1
        self._try_open(
            day, epic_index, candle, buf, intent, open_positions, closed_today, result
        )

    def _try_open(
        self,
        day: int,
        epic_index: int,
        candle: Candle,
        buf: EpicBuffer,
        intent,
        open_positions: dict[int, SimulatedTrade],
        closed_today: list[SimulatedTrade],
        result: SimulationResult,
    ) -> bool:
        """Run the pre-open gates + euro-risk check and open on success.

        The close profile (not the entry) chooses the initial protective stop
        and any take-profit via ``initial_plan`` — exactly as the live
        ``open_from_intent`` does.

        Returns True when a position was opened.
        """
        trade_count = len(closed_today)
        wins = sum(1 for t in closed_today if t.win)
        win_rate = wins / trade_count if trade_count else 1.0
        daily_pnl = sum(t.euro or 0.0 for t in closed_today)

        allowed, reason = evaluate_open_gates(
            epic=buf.epic,
            direction=intent.direction,
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
            return False

        plan = self._close_profile.initial_plan(
            entry_level=candle.bid_close, direction=intent.direction, buf=buf
        )
        stop_distance = candle.bid_close - plan.stop_level
        euro_per_point = self._sim.euro_per_point * self._sim.quantity
        euro_risk = stop_distance * euro_per_point
        if euro_risk > self._config.euro_loss_max:
            result.rejections["Euro risk too high"] += 1
            return False

        # A market BUY fills at the offer (same as IG's confirmation level).
        open_positions[epic_index] = SimulatedTrade(
            epic=buf.epic,
            day=day,
            open_time=candle.timestamp.strftime("%H:%M"),
            level_open=round(candle.offer_close, 5),
            level_win=round(plan.target_level, 5),
            level_zero=round(plan.level_zero, 5),
            level_loose=round(plan.stop_level, 5),
            level_stop=round(plan.stop_level, 5),
            level_follower=round(plan.stop_level, 5),
            level_margin=round(plan.level_margin, 5),
            euro_stop=round(euro_risk, 2),
            euro_per_point=euro_per_point,
            opened_at=candle.timestamp,
        )
        return True

    # ------------------------------------------------------------------ #
    # Monitoring / closing                                                #
    # ------------------------------------------------------------------ #

    def _monitor(
        self, position: SimulatedTrade, candle: Candle, buf: EpicBuffer
    ) -> bool:
        """Mirror of ``TradingService.manage_position`` + the broker-side stop.

        The close profile owns the per-tick decision (close / ratchet / hold);
        the broker-side stop models IG filling the pushed stop intra-candle.

        Returns True when the position was closed.
        """
        current_bid = candle.bid_close

        # Broker-side protective stop: the close profile owns the stop level
        # (``level_follower``), so this models IG filling the *pushed* stop when
        # the low touches it. The profile may move the stop down as well as up
        # (e.g. atr_trailing_positive giving a soft dip room), hence the level is
        # taken as-is rather than ratcheted here.
        broker_stop = position.level_follower
        if candle.bid_low <= broker_stop:
            reason = "follower" if position.stop_updates else "stop"
            self._close(position, candle, broker_stop, reason)
            return True

        decision = self._close_profile.evaluate(
            position,
            current_bid,
            buf,
            is_close_hour=candle.timestamp.hour >= self._config.hour_close,
        )
        if decision.action == ACTION_CLOSE:
            self._close(position, candle, current_bid, decision.reason)
            return True
        if (
            decision.action == ACTION_UPDATE_STOP
            and decision.new_stop_level is not None
        ):
            # Apply the profile's stop verbatim (up or down) — mirrors live
            # manage_position, which pushes whatever level the profile returns.
            position.level_follower = round(decision.new_stop_level, 5)
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
    close_profile_name: str | None = None,
) -> SimulationResult:
    """Wire the curve generator to the simulator and run it.

    The provider derives one deterministic seed per (day, epic) from the master
    seed, so a run is fully reproducible while every curve stays distinct. The
    entry strategy and close profile are resolved by name (decoupled); when a
    name is omitted the configured ``ENTRY_STRATEGY_NAME`` / ``CLOSE_PROFILE_NAME``
    is used — the simulator then replays exactly what the live bot would do.
    """
    from src.backtest.curve_generator import generate_curve
    from src.entry import get_entry_strategy
    from src.exit import get_close_profile

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
        entry=get_entry_strategy(
            strategy_name or settings.entry_strategy_name, settings
        ),
        close_profile=get_close_profile(
            close_profile_name or settings.close_profile_name, settings
        ),
        sim_config=sim_config,
        curve_provider=provider,
    )
    return simulator.run()


def run_close_visual(
    settings,
    *,
    curve_profile: str = "random",
    close_profile_name: str | None = None,
    seed: int | None = None,
    num_candles: int = 600,
    base_price: float = 8000.0,
    euro_per_point: float = 1.0,
    open_index: int | None = None,
) -> dict:
    """Replay a single open→close cycle on one curve to visualise the exit.

    Unlike :func:`run_simulation` (which aggregates hundreds of trades), this
    opens **one** BUY at a (random or chosen) moment on a single synthetic curve
    and walks the chosen close profile forward tick by tick, recording the
    protective stop level at every step. The result is everything the front-end
    needs to draw: the price curve, the entry marker, the trailing-stop line as
    it ratchets up, and the exit marker.

    The entry direction is fixed to BUY and the open is unconditional (no entry
    strategy, no gates): the point is to inspect the *close* behaviour in
    isolation — does the stop follow the curve with a safety gap and climb when
    price climbs, without closing on noise?
    """
    from src.backtest.curve_generator import generate_curve
    from src.exit import get_close_profile

    if seed is None:
        seed = random.randrange(2**31)

    candles = generate_curve(
        curve_profile,
        seed=seed,
        num_candles=num_candles,
        base_price=base_price,
        day=_BASE_DAY,
    )

    profile = get_close_profile(
        close_profile_name or settings.close_profile_name, settings
    )
    config = TradeConfig.from_settings(settings)

    # The open must sit far enough in to have a warmup window for the ATR, and
    # leave room afterwards for the curve to evolve and the stop to trail.
    warmup = max(settings.strategy_atr_period + 5, 30)
    last_open = max(warmup + 1, num_candles - 100)
    if open_index is None:
        open_index = random.Random(seed ^ 0x5EED).randrange(warmup, last_open)
    open_index = max(warmup, min(open_index, num_candles - 2))

    # Feed the warmup window into the buffer up to (and including) the open tick.
    buf = EpicBuffer(epic="SIM", max_candles=num_candles + 1)
    for candle in candles[: open_index + 1]:
        buf.add(candle)

    open_candle = candles[open_index]
    plan = profile.initial_plan(
        entry_level=open_candle.bid_close, direction="BUY", buf=buf
    )
    stop_distance = open_candle.bid_close - plan.stop_level
    euro_risk = stop_distance * euro_per_point

    # Reference levels for the chart, all expressed on the bid scale (the curve
    # plotted): break-even is the offer paid for a BUY (≈ one spread above the
    # opening bid), and the margin level (frozen on the plan at open) adds the
    # profile's noise margin on top — the threshold above which a position counts
    # as "positive beyond noise". Sourcing it from the plan keeps the drawn line
    # identical to the level the profile actually enforces.
    level_zero = round(plan.level_zero, 5)
    level_margin = round(plan.level_margin, 5) if plan.level_margin else level_zero
    noise_margin = round(level_margin - level_zero, 5)

    position = SimulatedTrade(
        epic="SIM",
        day=0,
        open_time=open_candle.timestamp.strftime("%H:%M"),
        level_open=round(open_candle.offer_close, 5),  # market BUY fills at offer
        level_win=round(plan.target_level, 5),
        level_zero=round(plan.level_zero, 5),
        level_loose=round(plan.stop_level, 5),
        level_stop=round(plan.stop_level, 5),
        level_follower=round(plan.stop_level, 5),
        level_margin=round(plan.level_margin, 5),
        euro_stop=round(euro_risk, 2),
        euro_per_point=euro_per_point,
        opened_at=open_candle.timestamp,
    )

    # The broker-side stop is the level the close profile owns (level_follower):
    # it may move up or down, and the broker fills it intra-candle on the low.
    stop_track: list[dict] = [{"index": open_index, "level": round(plan.stop_level, 5)}]
    close_index = num_candles - 1
    close_level = candles[-1].bid_close
    close_reason = "end_of_day"

    for idx in range(open_index + 1, num_candles):
        candle = candles[idx]
        buf.add(candle)
        current_bid = candle.bid_close

        broker_stop = position.level_follower
        if candle.bid_low <= broker_stop:
            close_index = idx
            close_level = broker_stop
            close_reason = "follower" if position.stop_updates else "stop"
            stop_track.append({"index": idx, "level": round(broker_stop, 5)})
            break

        decision = profile.evaluate(
            position,
            current_bid,
            buf,
            is_close_hour=candle.timestamp.hour >= config.hour_close,
        )
        if decision.action == ACTION_CLOSE:
            close_index = idx
            close_level = current_bid
            close_reason = decision.reason or "close"
            stop_track.append({"index": idx, "level": round(broker_stop, 5)})
            break
        if (
            decision.action == ACTION_UPDATE_STOP
            and decision.new_stop_level is not None
        ):
            # Apply the profile's stop verbatim (up or down), like live trading.
            position.level_follower = round(decision.new_stop_level, 5)
            position.stop_updates += 1

        stop_track.append({"index": idx, "level": round(position.level_follower, 5)})

    euro = round((close_level - position.level_open) * euro_per_point, 2)

    return {
        "curve_profile": curve_profile,
        "close_profile": profile.name,
        "seed": seed,
        "timestamps": [c.timestamp.strftime("%H:%M") for c in candles],
        "bids": [round(c.bid_close, 5) for c in candles],
        # Intra-candle bid range: a long's protective stop is filled on the low,
        # so the low must be plotted for a stop hit to be visible (the close line
        # alone can stay above a stop the wick already pierced).
        "bid_lows": [round(c.bid_low, 5) for c in candles],
        "bid_highs": [round(c.bid_high, 5) for c in candles],
        "offers": [round(c.offer_close, 5) for c in candles],
        "open": {
            "index": open_index,
            "time": position.open_time,
            "level": position.level_open,  # fill price (offer paid)
            "bid": round(open_candle.bid_close, 5),  # bid at open (on the curve)
            "initial_stop": round(plan.stop_level, 5),
        },
        "level_zero": level_zero,  # break-even on the bid scale
        "level_margin": level_margin,  # break-even + noise margin
        "noise_margin": round(noise_margin, 5),
        "stops": stop_track,
        "close": {
            "index": close_index,
            "time": candles[close_index].timestamp.strftime("%H:%M"),
            "level": round(close_level, 5),
            "reason": close_reason,
        },
        "stop_updates": position.stop_updates,
        "euro": euro,
        "win": euro > 0,
    }


def run_open_visual(
    settings,
    *,
    curve_profile: str = "random",
    strategy_name: str | None = None,
    seed: int | None = None,
    num_candles: int = 600,
    base_price: float = 8000.0,
) -> dict:
    """Replay one synthetic day until the entry strategy first decides to open.

    Counterpart to :func:`run_close_visual` for the *open* side: it walks the
    chosen :class:`~src.entry.base.EntryStrategy` forward tick by tick over a
    single synthetic curve (no gates, no exit) and stops at the **first BUY**
    :class:`~src.entry.base.EntryIntent` — the moment the live bot would open.

    To keep the assessment of *whether the trigger fires correctly* free of
    hindsight bias, the returned curve is **truncated at the open tick**: the
    future price action (which would reveal the outcome) is never sent to the
    front-end. When the strategy never opens over the whole day, the full curve
    is returned with ``opened`` false so the absence of a signal is itself
    visible.

    SELL intents are ignored, mirroring the live pipeline (the risk gate opens
    BUY only) and the aggregate simulator's open path.
    """
    from src.backtest.curve_generator import generate_curve
    from src.entry import get_entry_strategy

    if seed is None:
        seed = random.randrange(2**31)

    candles = generate_curve(
        curve_profile,
        seed=seed,
        num_candles=num_candles,
        base_price=base_price,
        day=_BASE_DAY,
    )

    entry = get_entry_strategy(strategy_name or settings.entry_strategy_name, settings)

    # Feed candles one at a time and evaluate exactly as the scheduler does:
    # only once the warmup window is filled, and stop on the first BUY.
    buf = EpicBuffer(epic="SIM", max_candles=num_candles + 1)
    open_index: int | None = None
    score = 0.0
    for idx, candle in enumerate(candles):
        buf.add(candle)
        if len(buf) < entry.warmup:
            continue
        intent = entry.evaluate(buf.epic, buf)
        if intent is not None and intent.direction == "BUY":
            open_index = idx
            score = round(intent.score, 4)
            break

    opened = open_index is not None
    # Truncate the future once an open is found; otherwise show the whole day so
    # the absence of any trigger is itself visible.
    last = (open_index + 1) if opened else num_candles

    return {
        "curve_profile": curve_profile,
        "strategy": entry.name,
        "seed": seed,
        "opened": opened,
        "warmup": entry.warmup,
        "candles_total": num_candles,
        "timestamps": [c.timestamp.strftime("%H:%M") for c in candles[:last]],
        "bids": [round(c.bid_close, 5) for c in candles[:last]],
        "offers": [round(c.offer_close, 5) for c in candles[:last]],
        "open": (
            {
                "index": open_index,
                "time": candles[open_index].timestamp.strftime("%H:%M"),
                "bid": round(candles[open_index].bid_close, 5),
                "offer": round(candles[open_index].offer_close, 5),
                "score": score,
                "direction": "BUY",
            }
            if opened
            else None
        ),
    }
