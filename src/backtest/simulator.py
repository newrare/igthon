"""Strategy simulator — replays the project's trading rules on synthetic curves.

Feeds fictional candles (see :mod:`src.backtest.curve_generator`) through the
exact same decoupled decision pipeline used live:

- open: an :class:`src.entry.base.EntryStrategy` produces a direction-only
  :class:`~src.entry.base.EntryIntent`;
- pre-open gates: :func:`src.execution.gates.evaluate_open_gates`;
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
from itertools import groupby

from src.entry.base import EntryStrategy
from src.execution.gates import evaluate_open_gates
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
    # Percentage lens: spread cost charged at each open, as a % of the entry bid.
    # The engine (stop placement, risk gate) is unaffected — this only sets the
    # per-trade malus subtracted from the bid→bid % return reported alongside the
    # euro figures (see SimulationResult.summary).
    spread_malus_pct: float = 0.0


@dataclass(slots=True)
class SimulatedTrade:
    """One simulated open/close cycle with the same levels as a live Position."""

    epic: str
    day: int
    open_time: str
    level_open: float
    level_zero: float
    level_loose: float
    level_stop: float  # broker-side protective stop (trails upward)
    euro_stop: float
    level_margin: float = 0.0  # margin level frozen at open (read by the profile)
    level_open_bid: float = 0.0  # bid at open (for the bid→bid % return lens)
    euro_per_point: float = 0.0  # € per point of movement (read by the profile)
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
    spread_malus_pct: float = 0.0  # % charged per open in the percentage lens

    def _net_pcts(self) -> list[float]:
        """Per-trade net return in %, bid→bid, minus the spread malus.

        The fictional-points lens the dashboard asks for: gross move measured on
        the bid scale (the offer fill is ignored here so the spread cost is *only*
        the explicit malus), then the per-open malus is subtracted::

            gross_pct = (level_close - level_open_bid) / level_open_bid * 100
            net_pct   = gross_pct - spread_malus_pct
        """
        pcts: list[float] = []
        for t in self.trades:
            if not t.level_open_bid or t.level_close is None:
                continue
            gross = (t.level_close - t.level_open_bid) / t.level_open_bid * 100.0
            pcts.append(gross - self.spread_malus_pct)
        return pcts

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

        # Percentage lens (fictional points): bid→bid return minus spread malus.
        net_pcts = self._net_pcts()
        win_pcts = [p for p in net_pcts if p > 0]
        loss_pcts = [p for p in net_pcts if p <= 0]
        equity_pct: list[float] = []
        total_pct = 0.0
        peak_pct = 0.0
        max_dd_pct = 0.0
        for p in net_pcts:
            total_pct += p
            peak_pct = max(peak_pct, total_pct)
            max_dd_pct = max(max_dd_pct, peak_pct - total_pct)
            equity_pct.append(round(total_pct, 4))
        n_pct = len(net_pcts)

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
            # Percentage lens — fictional points, spread charged as an explicit %.
            "spread_malus_pct": round(self.spread_malus_pct, 4),
            "wins_pct": len(win_pcts),
            "losses_pct": len(loss_pcts),
            "win_rate_pct": round(len(win_pcts) / n_pct, 4) if n_pct else 0.0,
            "total_pct": round(total_pct, 4),
            "avg_win_pct": (
                round(sum(win_pcts) / len(win_pcts), 4) if win_pcts else 0.0
            ),
            "avg_loss_pct": (
                round(sum(loss_pcts) / len(loss_pcts), 4) if loss_pcts else 0.0
            ),
            "best_pct": round(max(net_pcts), 4) if net_pcts else 0.0,
            "worst_pct": round(min(net_pcts), 4) if net_pcts else 0.0,
            "max_drawdown_pct": round(max_dd_pct, 4),
            "equity_pct": equity_pct,
            "net_pcts": [round(p, 4) for p in net_pcts],
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
        result = SimulationResult(spread_malus_pct=self._sim.spread_malus_pct)
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
        """Play one trading day, dispatching on the entry's selection model.

        A **cross-epic ranker** (``cross_epic_selection``, e.g.
        ``open_ranking``) keeps a single rolling position chosen as the
        best of all epics — handled by :meth:`_run_day_ranker`, a faithful
        mirror of the scheduler's ``_select_and_open``. A **per-epic gate**
        (``donchian_*``) opens whatever epic fires first — handled by
        :meth:`_run_day_gated`.
        """
        if self._entry.cross_epic_selection:
            self._run_day_ranker(day, curves, result)
        else:
            self._run_day_gated(day, curves, result)

    def _run_day_gated(
        self,
        day: int,
        curves: Sequence[tuple[str, list[Candle]]],
        result: SimulationResult,
    ) -> None:
        """Per-epic gate day: feed every epic's candles and open on first BUY.

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

    def _run_day_ranker(
        self,
        day: int,
        curves: Sequence[tuple[str, list[Candle]]],
        result: SimulationResult,
    ) -> None:
        """Cross-epic ranker day — a faithful mirror of ``_select_and_open``.

        Holds ``entry.concurrent_positions`` rolling positions (1 for
        ``open_ranking``). The merged event stream is processed **one
        timestamp at a time**: every epic's candle for that tick is ingested and
        open positions are monitored first (a close frees a slot); then, while a
        slot is free, *all* eligible epics are scored, ranked by score, and the
        best one that clears the open gates is opened. An epic opened earlier in
        the day is dropped from the candidate set (``used_today``) so the rolling
        position rotates across markets — exactly the scheduler's diversity rule.

        Scoring every epic only happens while a slot is free; in the steady state
        (slot filled) the tick is monitor-only, matching the live cheap path.
        """
        buffers = [EpicBuffer(epic=label) for label, _ in curves]
        open_positions: dict[int, SimulatedTrade] = {}
        closed_today: list[SimulatedTrade] = []
        last_candles: list[Candle | None] = [None] * len(curves)
        used_today: set[int] = set()  # epics already opened today (diversity rule)

        # Selection model knobs (see the scheduler's ``_select_and_open``):
        #  - wallet_bounded: no fixed concurrent-count cap. The live wallet gate
        #    (available funds − reserve) is its only limit, which the backtest
        #    cannot model (the archive holds prices, not account balance / margin),
        #    so the cap is lifted to the whole universe — "hold as many as
        #    qualify". The score floor and the open cooldown become the effective
        #    limiters, so the reported opens/day are an UPPER BOUND (the real
        #    wallet reserve would cap concurrent positions further).
        #  - open_cooldown_minutes: at most one open per cooldown window, so
        #    positions are staggered instead of fired in a burst.
        #  - allow_same_day_reopen: an epic re-enters the candidate pool as soon
        #    as it holds no open position (the diversity ``used_today`` filter is
        #    skipped), so the same market can be opened several times in one day.
        wallet_bounded = getattr(self._entry, "wallet_bounded", False)
        cooldown_min = int(getattr(self._entry, "open_cooldown_minutes", 0) or 0)
        allow_reopen = getattr(self._entry, "allow_same_day_reopen", False)
        target = (
            len(curves)
            if wallet_bounded
            else max(int(getattr(self._entry, "concurrent_positions", 1)), 1)
        )
        last_open_ts: datetime | None = None

        events = sorted(
            (
                (candle.timestamp, e, candle)
                for e, (_, candles) in enumerate(curves)
                for candle in candles
            ),
            key=lambda ev: (ev[0], ev[1]),
        )

        for ts, group in groupby(events, key=lambda ev: ev[0]):
            # Stop once the run target is met and nothing is left to wind down.
            if len(result.trades) >= self._sim.target_trades and not open_positions:
                break

            # 1. Ingest this tick's candles and monitor open positions; a close
            #    here frees a slot the selection step below can immediately refill.
            for _ts2, e, candle in group:
                buffers[e].add(candle)
                last_candles[e] = candle
                position = open_positions.get(e)
                if position is not None and self._monitor(position, candle, buffers[e]):
                    del open_positions[e]
                    closed_today.append(position)
                    result.trades.append(position)

            # 2. Refill free slots with the best-ranked eligible epics.
            last_open_ts = self._select_and_open(
                ts,
                day,
                buffers,
                last_candles,
                open_positions,
                closed_today,
                used_today,
                target,
                result,
                cooldown_min=cooldown_min,
                allow_reopen=allow_reopen,
                last_open_ts=last_open_ts,
            )

        # Force-close anything still open at the end of the day.
        for e, position in list(open_positions.items()):
            candle = last_candles[e]
            if candle is not None:
                self._close(position, candle, candle.bid_close, "end_of_day")
                result.trades.append(position)

    def _select_and_open(
        self,
        ts: datetime,
        day: int,
        buffers: list[EpicBuffer],
        last_candles: list[Candle | None],
        open_positions: dict[int, SimulatedTrade],
        closed_today: list[SimulatedTrade],
        used_today: set[int],
        target: int,
        result: SimulationResult,
        *,
        cooldown_min: int = 0,
        allow_reopen: bool = False,
        last_open_ts: datetime | None = None,
    ) -> datetime | None:
        """Score every eligible epic, rank by score, open the best into free slots.

        Mirror of the scheduler's rolling selector: candidates are epics that are
        not already open, not used earlier today (unless ``allow_reopen``), and
        have enough buffered history to score. They are ranked highest-score-first
        and opened (through the same gates as ``_run_day_gated``) until the target
        position count is reached.

        ``cooldown_min`` (> 0) paces the opens: while less than that many minutes
        have elapsed since ``last_open_ts`` the pass is a cheap no-op (no scoring),
        and when it does open it takes at most one position — so positions are
        staggered exactly as the live cooldown does. Returns the (possibly updated)
        ``last_open_ts`` for the caller to carry to the next tick.
        """
        slots = target - len(open_positions)
        if slots <= 0:
            return last_open_ts  # target met — cheap path

        # Open cooldown: at most one open per window; skip scoring entirely while
        # the window is still open (mirrors the scheduler's early return).
        if (
            cooldown_min > 0
            and last_open_ts is not None
            and (ts - last_open_ts).total_seconds() < cooldown_min * 60
        ):
            return last_open_ts

        ranked: list[tuple] = []
        for e, buf in enumerate(buffers):
            if e in open_positions:
                continue
            if not allow_reopen and e in used_today:
                continue
            if len(buf) < self._entry.warmup:
                continue
            intent = self._entry.evaluate(buf.epic, buf)
            if intent is not None and intent.direction == "BUY":
                ranked.append((intent.score, e, intent))
        if not ranked:
            return last_open_ts
        ranked.sort(key=lambda item: item[0], reverse=True)
        result.buy_signals += len(ranked)

        # A paced strategy opens at most one position per cooldown window.
        max_opens = 1 if cooldown_min > 0 else slots
        opened = 0
        for _score, e, intent in ranked:
            if slots <= 0 or opened >= max_opens:
                break
            candle = last_candles[e]
            if candle is None:
                continue
            if self._try_open(
                day,
                e,
                candle,
                buffers[e],
                intent,
                open_positions,
                closed_today,
                result,
            ):
                used_today.add(e)
                slots -= 1
                opened += 1
                last_open_ts = ts
        return last_open_ts

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
        """Run the pre-open gates and open on success.

        The close profile (not the entry) chooses the initial protective stop
        and any take-profit via ``initial_plan`` — exactly as the live
        ``open_from_intent`` does.

        Returns True when a position was opened.
        """
        allowed, reason = evaluate_open_gates(
            epic=buf.epic,
            direction=intent.direction,
            in_trading_hours=True,
            epic_already_open=epic_index in open_positions,
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

        # A market BUY fills at the offer (same as IG's confirmation level).
        open_positions[epic_index] = SimulatedTrade(
            epic=buf.epic,
            day=day,
            open_time=candle.timestamp.strftime("%H:%M"),
            level_open=round(candle.offer_close, 5),
            level_open_bid=round(candle.bid_close, 5),
            level_zero=round(plan.level_zero, 5),
            level_loose=round(plan.stop_level, 5),
            level_stop=round(plan.stop_level, 5),
            level_follower=round(plan.stop_level, 5),
            level_margin=round(plan.level_margin, 5),
            euro_stop=round(euro_risk, 2),
            euro_per_point=euro_per_point,
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
        # the low touches it. The profile may in principle move the stop down as
        # well as up (a zone updater could give a soft dip room), hence the level
        # is taken as-is rather than ratcheted here.
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
    entry strategy is resolved by name (``strategy_name`` or the configured
    ``OPEN_STRATEGY``). The exit is the single composer profile built from
    settings — its per-zone behaviour comes from the ``CLOSE_ZONE*`` selectors, so
    ``close_profile_name`` is accepted for API compatibility but not used for
    selection. The simulator thus replays exactly what the live bot would do.
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
        entry=get_entry_strategy(strategy_name or settings.open_strategy, settings),
        close_profile=get_close_profile(settings),
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

    profile = get_close_profile(settings)
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
        level_open_bid=round(open_candle.bid_close, 5),
        level_zero=round(plan.level_zero, 5),
        level_loose=round(plan.stop_level, 5),
        level_stop=round(plan.stop_level, 5),
        level_follower=round(plan.stop_level, 5),
        level_margin=round(plan.level_margin, 5),
        euro_stop=round(euro_risk, 2),
        euro_per_point=euro_per_point,
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

    entry = get_entry_strategy(strategy_name or settings.open_strategy, settings)

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
